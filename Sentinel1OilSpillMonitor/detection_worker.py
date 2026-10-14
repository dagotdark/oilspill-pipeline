import json
import logging
import os
import time
from pathlib import Path

import psycopg2
import requests

from main import RESULTS_DRIVE
from settings import load_env, require_env


# ============================================================
# CONFIG
# ============================================================

# Секреты лежат в .env рядом со скриптом (образец — .env.example)
load_env()

DB_CONFIG = {
    "dbname": "data_api",
    "user": "data_api_user",
    "password": require_env("DB_PASSWORD"),
    "host": "79.143.20.17",
    "port": 5432,
}

LARAVEL_API_URL = "https://data-api.igmass.kz/api"

LARAVEL_EMAIL = "detection_worker@test.com"
LARAVEL_PASSWORD = require_env("DETECTION_WORKER_PASSWORD")

SENTINEL_API_URL = "http://127.0.0.1:5554"

# Папка самого Sentinel1OilSpillMonitor
BASE_DIR = Path(__file__).resolve().parent

# Куда Sentinel ожидает входные TIFF
INPUTS_DIR = BASE_DIR / "inputs"

# Как часто проверять очередь
POLL_INTERVAL = 5

# Максимальное время ожидания filtered.geojson
DETECTION_TIMEOUT = 3600

# HTTP timeout для отдельных HTTP-запросов
HTTP_TIMEOUT = 120


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("detection-worker")


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def get_status_id(conn, code):
    """
    Получить ID статуса по его code.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM oil_spill_statuses
            WHERE code = %s
            LIMIT 1
            """,
            (code,),
        )

        row = cur.fetchone()

        if row is None:
            raise RuntimeError(
                f"Oil spill status not found: {code}"
            )

        return row[0]


# ============================================================
# CLAIM ANALYSIS
# ============================================================

def claim_analysis():
    """
    Найти одну waiting_detection задачу и атомарно
    перевести её в detecting.

    Транзакция закрывается сразу после захвата.
    """

    conn = get_db_connection()

    try:
        conn.autocommit = False

        waiting_status_id = get_status_id(
            conn,
            "waiting_detection",
        )

        detecting_status_id = get_status_id(
            conn,
            "detecting",
        )

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    a.id,
                    a.file_name,
                    a.file_path
                FROM oil_spill_analyses AS a
                WHERE a.status_id = %s
                ORDER BY a.id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (waiting_status_id,),
            )

            row = cur.fetchone()

            if row is None:
                conn.rollback()
                return None

            analysis_id = row[0]
            file_name = row[1]
            file_path = row[2]

            cur.execute(
                """
                UPDATE oil_spill_analyses
                SET
                    status_id = %s,
                    detection_started_at = NOW(),
                    error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    detecting_status_id,
                    analysis_id,
                ),
            )

        conn.commit()

        analysis = {
            "id": analysis_id,
            "file_name": file_name,
            "file_path": file_path,
        }

        logger.info(
            "Analysis %s claimed: %s",
            analysis_id,
            file_name,
        )

        return analysis

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# LARAVEL AUTHENTICATION
# ============================================================

def login_laravel():
    """
    Авторизация в Laravel API.
    """

    logger.info("Logging in to Laravel API...")

    response = requests.post(
        f"{LARAVEL_API_URL}/login",
        json={
            "email": LARAVEL_EMAIL,
            "password": LARAVEL_PASSWORD,
        },
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    token = (
        data.get("token")
        or data.get("access_token")
    )

    if not token:
        raise RuntimeError(
            "Laravel login response does not contain token: "
            f"{data}"
        )

    logger.info(
        "Laravel authentication successful"
    )

    return token


# ============================================================
# DOWNLOAD TIFF
# ============================================================

def download_tiff(analysis, token):
    """
    Скачать TIFF через Laravel API и сохранить
    в Sentinel1OilSpillMonitor/inputs/.
    """

    analysis_id = analysis["id"]
    file_name = analysis["file_name"]

    INPUTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination = INPUTS_DIR / file_name

    logger.info(
        "Downloading TIFF for analysis %s: %s",
        analysis_id,
        file_name,
    )

    response = requests.get(
        f"{LARAVEL_API_URL}/oil-spill-analyses/{analysis_id}/file",
        headers={
            "Authorization": f"Bearer {token}",
        },
        stream=True,
        timeout=HTTP_TIMEOUT,
    )

    # Если токен протух
    if response.status_code == 401:
        raise PermissionError(
            "Laravel API returned 401 Unauthorized"
        )

    response.raise_for_status()

    try:
        with open(destination, "wb") as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

    except Exception:

        # Если скачивание оборвалось —
        # удаляем недокачанный файл.
        if destination.exists():
            destination.unlink()

        raise

    if not destination.exists():
        raise RuntimeError(
            f"Downloaded TIFF does not exist: {destination}"
        )

    file_size = destination.stat().st_size

    if file_size == 0:
        destination.unlink()

        raise RuntimeError(
            f"Downloaded TIFF is empty: {destination}"
        )

    logger.info(
        "TIFF downloaded successfully: %s (%.2f MB)",
        destination,
        file_size / 1024 / 1024,
    )

    return destination


# ============================================================
# RUN SENTINEL DETECTION
# ============================================================

def run_detection(tiff_path):
    """
    Вызвать существующий Sentinel1OilSpillMonitor API.
    """

    file_name = tiff_path.name

    sentinel_file_name = f"inputs/{file_name}"

    logger.info(
        "Starting Sentinel detection: %s",
        sentinel_file_name,
    )

    response = requests.post(
        f"{SENTINEL_API_URL}/segment",
        params={
            "file_name": sentinel_file_name,
        },
        headers={
            "accept": "application/json",
        },
        data="",
        timeout=DETECTION_TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    logger.info(
        "Sentinel response: %s",
        data,
    )

    if data.get("status") != 200:
        raise RuntimeError(
            "Sentinel detection returned unsuccessful status: "
            f"{data}"
        )

    logger.info(
        "Sentinel detection started successfully"
    )


# ============================================================
# FIND RESULT GEOJSON
# ============================================================

def get_filtered_geojson_path(tiff_path):
    """
    Сформировать путь к:
    {RESULTS_DRIVE}/{base_name}/segmentation/
    {base_name}_filtered.geojson
    """

    base_name = tiff_path.stem

    result_folder = os.path.join(
        RESULTS_DRIVE,
        base_name,
    )

    geojson_path = os.path.join(
        result_folder,
        "segmentation",
        f"{base_name}_filtered.geojson",
    )

    return Path(geojson_path)


# ============================================================
# WAIT FOR GEOJSON
# ============================================================

def wait_for_geojson(tiff_path):
    """
    Ждать появления filtered.geojson с timeout.
    """

    geojson_path = get_filtered_geojson_path(
        tiff_path
    )

    logger.info(
        "Waiting for Detection result: %s",
        geojson_path,
    )

    started_at = time.monotonic()

    while True:

        if geojson_path.is_file():

            logger.info(
                "Detection GeoJSON found: %s",
                geojson_path,
            )

            return geojson_path

        elapsed = time.monotonic() - started_at

        if elapsed >= DETECTION_TIMEOUT:

            raise TimeoutError(
                "Detection timeout. "
                f"Result was not created within "
                f"{DETECTION_TIMEOUT} seconds: "
                f"{geojson_path}"
            )

        time.sleep(5)


# ============================================================
# IMPORT GEOJSON
# ============================================================

def import_geojson(analysis_id, geojson_path):
    """
    Импортировать Features из filtered.geojson
    в oil_spill_detections.
    """

    logger.info(
        "Importing GeoJSON: %s",
        geojson_path,
    )

    with open(
        geojson_path,
        "r",
        encoding="utf-8",
    ) as file:
        geojson = json.load(file)

    if geojson.get("type") != "FeatureCollection":
        raise RuntimeError(
            "Detection GeoJSON is not a FeatureCollection"
        )

    features = geojson.get("features", [])

    logger.info(
        "Found %d Detection features",
        len(features),
    )

    conn = get_db_connection()

    try:
        conn.autocommit = False

        inserted = 0

        with conn.cursor() as cur:

            for index, feature in enumerate(
                features,
                start=1,
            ):

                geometry = feature.get("geometry")
                properties = feature.get("properties") or {}

                if geometry is None:
                    logger.warning(
                        "Feature %d has no geometry, skipped",
                        index,
                    )
                    continue

                geometry_type = geometry.get("type")

                if geometry_type != "Polygon":
                    logger.warning(
                        "Feature %d has geometry type %s, "
                        "expected Polygon. Skipped.",
                        index,
                        geometry_type,
                    )
                    continue

                class_value = properties.get("class")

                if class_value is None:
                    logger.warning(
                        "Feature %d has no class, skipped",
                        index,
                    )
                    continue

                cur.execute(
                    """
                    INSERT INTO oil_spill_detections
                    (
                        analysis_id,
                        geometry,
                        class,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES
                    (
                        %s,
                        ST_SetSRID(
                            ST_GeomFromGeoJSON(%s),
                            4326
                        ),
                        %s,
                        %s,
                        NOW(),
                        NOW()
                    )
                    """,
                    (
                        analysis_id,
                        json.dumps(geometry),
                        class_value,
                        "detected",
                    ),
                )

                inserted += 1

        conn.commit()

        logger.info(
            "Imported %d Detection polygons for analysis %s",
            inserted,
            analysis_id,
        )

        return inserted

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# UPDATE SUCCESS STATUS
# ============================================================

def mark_detection_success(analysis_id):
    """
    detecting -> reviewing
    """

    conn = get_db_connection()

    try:
        conn.autocommit = False

        reviewing_status_id = get_status_id(
            conn,
            "reviewing",
        )

        detecting_status_id = get_status_id(
            conn,
            "detecting",
        )

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE oil_spill_analyses
                SET
                    status_id = %s,
                    detection_finished_at = NOW(),
                    error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status_id = %s
                """,
                (
                    reviewing_status_id,
                    analysis_id,
                    detecting_status_id,
                ),
            )

            if cur.rowcount != 1:
                raise RuntimeError(
                    f"Could not change analysis {analysis_id} "
                    f"from detecting to reviewing"
                )

        conn.commit()

        logger.info(
            "Analysis %s status changed to reviewing",
            analysis_id,
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# UPDATE FAILED STATUS
# ============================================================

def mark_detection_failed(
    analysis_id,
    error,
):
    """
    detecting -> detection_failed
    """

    conn = get_db_connection()

    try:
        conn.autocommit = False

        failed_status_id = get_status_id(
            conn,
            "detection_failed",
        )

        error_text = str(error)

        if len(error_text) > 10000:
            error_text = error_text[:10000]

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE oil_spill_analyses
                SET
                    status_id = %s,
                    detection_finished_at = NOW(),
                    error = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    failed_status_id,
                    error_text,
                    analysis_id,
                ),
            )

        conn.commit()

        logger.info(
            "Analysis %s status changed to detection_failed",
            analysis_id,
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# CLEANUP INPUT FILE
# ============================================================

def cleanup_input_file(tiff_path):
    """
    Удалить скачанный TIFF после завершения Detection.

    Пока НЕ удаляем автоматически.
    Файл оставляем в inputs для возможности проверки.
    """

    logger.info(
        "Keeping input TIFF: %s",
        tiff_path,
    )


# ============================================================
# PROCESS ONE ANALYSIS
# ============================================================

def process_analysis(
    analysis,
    token,
):
    """
    Полный цикл обработки одной Analysis.
    """

    analysis_id = analysis["id"]

    tiff_path = None

    try:

        # ----------------------------------------------------
        # 1. Download TIFF
        # ----------------------------------------------------

        tiff_path = download_tiff(
            analysis,
            token,
        )

        # ----------------------------------------------------
        # 2. Run Sentinel detection
        # ----------------------------------------------------

        run_detection(
            tiff_path,
        )

        # ----------------------------------------------------
        # 3. Wait for filtered GeoJSON
        # ----------------------------------------------------

        geojson_path = wait_for_geojson(
            tiff_path,
        )

        # ----------------------------------------------------
        # 4. Import Features into PostGIS
        # ----------------------------------------------------

        import_geojson(
            analysis_id,
            geojson_path,
        )

        # ----------------------------------------------------
        # 5. detecting -> reviewing
        # ----------------------------------------------------

        mark_detection_success(
            analysis_id,
        )

        logger.info(
            "Detection completed successfully for analysis %s",
            analysis_id,
        )

    except Exception as exc:

        logger.exception(
            "Detection failed for analysis %s",
            analysis_id,
        )

        try:

            mark_detection_failed(
                analysis_id,
                exc,
            )

        except Exception:

            logger.exception(
                "Could not mark analysis %s as detection_failed",
                analysis_id,
            )


# ============================================================
# MAIN WORKER
# ============================================================

def main():

    logger.info(
        "=============================================="
    )

    logger.info(
        "Sentinel1OilSpillMonitor Detection Worker"
    )

    logger.info(
        "=============================================="
    )

    logger.info(
        "BASE_DIR: %s",
        BASE_DIR,
    )

    logger.info(
        "INPUTS_DIR: %s",
        INPUTS_DIR,
    )

    logger.info(
        "RESULTS_DRIVE: %s",
        RESULTS_DRIVE,
    )

    logger.info(
        "Sentinel API: %s",
        SENTINEL_API_URL,
    )

    logger.info(
        "Laravel API: %s",
        LARAVEL_API_URL,
    )

    token = None

    while True:

        try:

            # ------------------------------------------------
            # Find and claim next task
            # ------------------------------------------------

            analysis = claim_analysis()

            if analysis is None:

                time.sleep(
                    POLL_INTERVAL
                )

                continue

            # ------------------------------------------------
            # Login if necessary
            # ------------------------------------------------

            if token is None:

                token = login_laravel()

            # ------------------------------------------------
            # Process
            # ------------------------------------------------

            process_analysis(
                analysis,
                token,
            )

        except PermissionError:

            logger.exception(
                "Laravel authentication failed"
            )

            token = None

            time.sleep(
                POLL_INTERVAL
            )

        except requests.HTTPError as exc:

            logger.exception(
                "HTTP error"
            )

            if (
                exc.response is not None
                and exc.response.status_code == 401
            ):
                token = None

            time.sleep(
                POLL_INTERVAL
            )

        except KeyboardInterrupt:

            logger.info(
                "Detection Worker stopped"
            )

            break

        except Exception:

            logger.exception(
                "Unexpected Worker error"
            )

            time.sleep(
                POLL_INTERVAL
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()