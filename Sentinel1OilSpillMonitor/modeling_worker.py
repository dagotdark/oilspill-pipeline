import json
import logging
import os
import re
import time
from pathlib import Path

import psycopg2
import requests
import rasterio
from rasterio.features import rasterize
from osgeo import ogr, osr

from main import (
    RESULTS_DRIVE,
    aggregate_to_km_resolution,
    tiff_to_xyz,
    extract_timestamp_from_filename,
)
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

LARAVEL_EMAIL = "modeling_worker@test.com"
LARAVEL_PASSWORD = require_env("MODELING_WORKER_PASSWORD")

MIKE_API_URL = "http://127.0.0.1:8000"

# Папка проекта
BASE_DIR = Path(__file__).resolve().parent

# Сюда скачивается исходный TIFF
INPUTS_DIR = BASE_DIR / "inputs"

# Интервал проверки очереди
POLL_INTERVAL = 5

# HTTP timeout
HTTP_TIMEOUT = 120

# Максимальное время ожидания MIKE
MIKE_RESULTS_TIMEOUT = 3600

# Интервал проверки MIKE
MIKE_RESULTS_INTERVAL = 10

# MIKE обязан создать именно 4 GeoJSON
EXPECTED_MIKE_FILES = 4


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("modeling-worker")


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def get_status_id(conn, code):
    """
    Получить ID статуса по code.
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
    Найти одну Analysis со статусом ready_for_modeling
    и атомарно перевести её в modeling.

    Используется:

        SELECT ... FOR UPDATE SKIP LOCKED
    """

    conn = get_db_connection()

    try:

        conn.autocommit = False

        ready_status_id = get_status_id(
            conn,
            "ready_for_modeling",
        )

        modeling_status_id = get_status_id(
            conn,
            "modeling",
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
                (
                    ready_status_id,
                ),
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
                    modeling_started_at = NOW(),
                    modeling_finished_at = NULL,
                    error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status_id = %s
                """,
                (
                    modeling_status_id,
                    analysis_id,
                    ready_status_id,
                ),
            )

            if cur.rowcount != 1:

                raise RuntimeError(
                    f"Could not claim analysis {analysis_id}"
                )

        conn.commit()

        analysis = {
            "id": analysis_id,
            "file_name": file_name,
            "file_path": file_path,
        }

        logger.info(
            "Analysis %s claimed for modeling: %s",
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

    logger.info(
        "Logging in to Laravel API..."
    )

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

def download_tiff(
    analysis,
    token,
):
    """
    Скачать исходный TIFF через Laravel API.
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

    if response.status_code == 401:

        raise PermissionError(
            "Laravel API returned 401 Unauthorized"
        )

    response.raise_for_status()

    try:

        with open(
            destination,
            "wb",
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:
                    output.write(chunk)

    except Exception:

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
# GET DETECTIONS
# ============================================================

def get_detections(
    analysis_id,
    token,
):
    """
    Получить Detection через Laravel API.

    Endpoint:

        /oil-spill-detections/filter?analysis_id=ID
    """

    logger.info(
        "Loading detections for analysis %s",
        analysis_id,
    )

    response = requests.get(
        f"{LARAVEL_API_URL}/oil-spill-detections/filter",
        params={
            "analysis_id": analysis_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code == 401:

        raise PermissionError(
            "Laravel API returned 401 Unauthorized"
        )

    response.raise_for_status()

    data = response.json()

    logger.info(
        "Detection API response received for analysis %s",
        analysis_id,
    )

    return data


# ============================================================
# EXTRACT CLASS == 1
# ============================================================

def extract_class_1_geojson(data):
    """
    Оставить только Detection:

        properties.class == 1

    status не проверяется.
    """

    geojson = data

    # --------------------------------------------------------
    # Обычный FeatureCollection
    # --------------------------------------------------------

    if (
        isinstance(data, dict)
        and data.get("type") == "FeatureCollection"
    ):

        geojson = data

    # --------------------------------------------------------
    # Ответ вида:
    #
    # {
    #     "data": {
    #         "type": "FeatureCollection",
    #         ...
    #     }
    # }
    # --------------------------------------------------------

    elif isinstance(data, dict):

        nested_data = data.get("data")

        if (
            isinstance(nested_data, dict)
            and nested_data.get("type") == "FeatureCollection"
        ):

            geojson = nested_data

    if not isinstance(geojson, dict):

        raise RuntimeError(
            "Invalid Detection API response"
        )

    if geojson.get("type") != "FeatureCollection":

        raise RuntimeError(
            "Detection API response is not a FeatureCollection: "
            f"{geojson}"
        )

    features = geojson.get(
        "features",
        [],
    )

    class_1_features = []

    class_counter = {}

    for index, feature in enumerate(
        features,
        start=1,
    ):

        properties = (
            feature.get("properties")
            or {}
        )

        class_value = properties.get(
            "class"
        )

        class_key = str(class_value)

        class_counter[class_key] = (
            class_counter.get(class_key, 0) + 1
        )

        # ----------------------------------------------------
        # Главное условие
        # ----------------------------------------------------

        if class_value != 1:
            continue

        geometry = feature.get(
            "geometry"
        )

        if not geometry:

            logger.warning(
                "Feature %d with class=1 has no geometry, skipped",
                index,
            )

            continue

        class_1_features.append(
            feature
        )

    logger.info(
        "Detection summary: total=%d, classes=%s",
        len(features),
        class_counter,
    )

    logger.info(
        "Detection polygons with class=1: %d",
        len(class_1_features),
    )

    if not class_1_features:

        raise RuntimeError(
            "No Detection polygons with class=1 found"
        )

    return {
        "type": "FeatureCollection",
        "features": class_1_features,
    }


# ============================================================
# GEOJSON -> MASK TIFF
# ============================================================

def geojson_to_mask_tif(
    geojson,
    src_tif,
    output_tif,
):
    """
    Преобразовать полигоны GeoJSON в бинарную маску TIFF.

    GeoJSON:
        EPSG:4326

    Маска:
        CRS исходного TIFF.
    """

    logger.info(
        "Creating mask TIFF: %s",
        output_tif,
    )

    # --------------------------------------------------------
    # Читаем CRS исходного TIFF
    # --------------------------------------------------------

    with rasterio.open(src_tif) as src:

        if src.crs is None:

            raise RuntimeError(
                f"Source TIFF has no CRS: {src_tif}"
            )

        destination_crs = src.crs

        logger.info(
            "Source TIFF CRS: %s",
            destination_crs,
        )

    # --------------------------------------------------------
    # Source CRS = EPSG:4326
    # --------------------------------------------------------

    src_srs = osr.SpatialReference()

    src_srs.ImportFromEPSG(
        4326
    )

    src_srs.SetAxisMappingStrategy(
        osr.OAMS_TRADITIONAL_GIS_ORDER
    )

    # --------------------------------------------------------
    # Destination CRS
    # --------------------------------------------------------

    dst_srs = osr.SpatialReference()

    dst_srs.ImportFromWkt(
        destination_crs.to_wkt()
    )

    dst_srs.SetAxisMappingStrategy(
        osr.OAMS_TRADITIONAL_GIS_ORDER
    )

    transform = osr.CoordinateTransformation(
        src_srs,
        dst_srs,
    )

    shapes = []

    # --------------------------------------------------------
    # GeoJSON polygons
    # --------------------------------------------------------

    for index, feature in enumerate(
        geojson.get("features", []),
        start=1,
    ):

        geometry = feature.get(
            "geometry"
        )

        if not geometry:

            logger.warning(
                "Feature %d has no geometry, skipped",
                index,
            )

            continue

        ogr_geometry = (
            ogr.CreateGeometryFromJson(
                json.dumps(geometry)
            )
        )

        if ogr_geometry is None:

            logger.warning(
                "Feature %d has invalid geometry, skipped",
                index,
            )

            continue

        try:

            ogr_geometry.Transform(
                transform
            )

        except Exception as exc:

            logger.warning(
                "Could not transform feature %d: %s",
                index,
                exc,
            )

            continue

        transformed_geometry = json.loads(
            ogr_geometry.ExportToJson()
        )

        shapes.append(
            (
                transformed_geometry,
                1,
            )
        )

    if not shapes:

        raise RuntimeError(
            "No valid geometries available for mask"
        )

    logger.info(
        "Rasterizing %d class=1 polygons",
        len(shapes),
    )

    # --------------------------------------------------------
    # Rasterize
    # --------------------------------------------------------

    with rasterio.open(
        src_tif
    ) as src:

        profile = src.profile.copy()

        profile.update(
            dtype="uint8",
            count=1,
            compress="lzw",
            nodata=0,
        )

        mask = rasterize(
            shapes=shapes,
            out_shape=(
                src.height,
                src.width,
            ),
            transform=src.transform,
            fill=0,
            dtype="uint8",
        )

        with rasterio.open(
            output_tif,
            "w",
            **profile,
        ) as dst:

            dst.write(
                mask,
                1,
            )

    logger.info(
        "Mask TIFF created successfully: %s",
        output_tif,
    )


# ============================================================
# SNAPSHOT MIKE GEOJSON FILES
# ============================================================

def snapshot_mike_files(result_folder):
    """
    Запомнить существующие GeoJSON перед запуском MIKE.

    Старые результаты не должны быть приняты
    за результаты текущего запуска.
    """

    geojson_dir = (
        Path(result_folder)
        / "mike"
        / "geojson"
    )

    snapshot = {}

    if not geojson_dir.is_dir():
        return snapshot

    for filepath in geojson_dir.iterdir():

        if not filepath.is_file():
            continue

        if filepath.suffix.lower() != ".geojson":
            continue

        try:

            stat = filepath.stat()

            snapshot[str(filepath)] = (
                stat.st_mtime,
                stat.st_size,
            )

        except OSError:
            continue

    logger.info(
        "Existing MIKE GeoJSON before start: %d",
        len(snapshot),
    )

    return snapshot


# ============================================================
# VALIDATE GEOJSON
# ============================================================

def is_valid_geojson(filepath):
    """
    Проверить GeoJSON.
    """

    try:

        if not filepath.is_file():
            return False

        if filepath.stat().st_size == 0:
            return False

        with open(
            filepath,
            "r",
            encoding="utf-8",
        ) as f:

            geojson = json.load(f)

        if geojson.get(
            "type"
        ) != "FeatureCollection":

            return False

        if "features" not in geojson:

            return False

        return True

    except Exception as exc:

        logger.warning(
            "Could not validate GeoJSON %s: %s",
            filepath,
            exc,
        )

        return False


# ============================================================
# WAIT FOR MIKE RESULTS
# ============================================================

def wait_for_mike_results(
    result_folder,
    previous_files,
    expected_files=4,
    timeout=10800,
    interval=30,
):
    """
    Ждать минимум expected_files новых/изменённых GeoJSON.
    """

    geojson_dir = (
        Path(result_folder)
        / "mike"
        / "geojson"
    )

    logger.info(
        "Waiting for MIKE results:"
    )

    logger.info(
        "%s",
        geojson_dir,
    )

    start_time = time.time()

    while True:

        elapsed = int(
            time.time() - start_time
        )

        valid_files = []

        if geojson_dir.is_dir():

            for filepath in geojson_dir.iterdir():

                if not filepath.is_file():
                    continue

                if filepath.suffix.lower() != ".geojson":
                    continue

                try:

                    stat = filepath.stat()

                except OSError:

                    continue

                current_mtime = stat.st_mtime
                current_size = stat.st_size

                # ------------------------------------------------
                # Старый файл должен быть изменён
                # ------------------------------------------------

                previous = previous_files.get(
                    str(filepath)
                )

                if previous is not None:

                    previous_mtime = previous[0]
                    previous_size = previous[1]

                    if (
                        current_mtime <= previous_mtime
                        and current_size == previous_size
                    ):

                        continue

                # ------------------------------------------------
                # Не принимать пустые файлы
                # ------------------------------------------------

                if current_size == 0:
                    continue

                # ------------------------------------------------
                # Проверяем GeoJSON
                # ------------------------------------------------

                if not is_valid_geojson(
                    filepath
                ):

                    continue

                valid_files.append(
                    filepath
                )

        logger.info(
            "Elapsed %d sec. "
            "New/updated valid MIKE GeoJSON: %d/%d",
            elapsed,
            len(valid_files),
            expected_files,
        )

        # --------------------------------------------------------
        # Обязательно 4 файла
        # --------------------------------------------------------

        if len(valid_files) >= expected_files:

            valid_files.sort()

            logger.info(
                "MIKE modeling completed."
            )

            for filepath in valid_files:

                logger.info(
                    "MIKE result: %s",
                    filepath,
                )

            return valid_files

        # --------------------------------------------------------
        # Timeout
        # --------------------------------------------------------

        if elapsed >= timeout:

            raise TimeoutError(
                f"MIKE did not create "
                f"{expected_files} new/updated valid GeoJSON "
                f"within {timeout} seconds: "
                f"{geojson_dir}"
            )

        time.sleep(
            interval
        )


# ============================================================
# RUN MIKE MODEL
# ============================================================

def run_model(
    tiff_path,
    mask_tif,
    result_1km_tif,
    result_folder,
):
    """
    Подготовить данные и запустить MIKE.

    Этапы:

        mask TIFF
            ↓
        1 km TIFF
            ↓
        XYZ
            ↓
        /calc_oil_spill
            ↓
        4 GeoJSON
    """

    # --------------------------------------------------------
    # 1. Mask -> 1 km TIFF
    # --------------------------------------------------------

    logger.info(
        "Aggregating mask to 1 km resolution..."
    )

    aggregate_to_km_resolution(
        str(mask_tif),
        str(result_1km_tif),
    )

    logger.info(
        "1 km raster created: %s",
        result_1km_tif,
    )

    # --------------------------------------------------------
    # 2. 1 km TIFF -> XYZ
    # --------------------------------------------------------

    logger.info(
        "Converting 1 km raster to XYZ..."
    )

    xyz = tiff_to_xyz(
        str(result_1km_tif)
    )

    if xyz.size == 0:

        raise RuntimeError(
            "Модель не нашла разлив нефти: XYZ is empty"
        )

    result_xyz = [
        list(x)
        for x in xyz
    ]

    logger.info(
        "XYZ points: %d",
        len(result_xyz),
    )

    # --------------------------------------------------------
    # 3. Date
    # --------------------------------------------------------

    time_1km = (
        extract_timestamp_from_filename(
            str(result_1km_tif)
        )
    )

    logger.info(
        "Model source datetime: %s",
        time_1km,
    )

    # --------------------------------------------------------
    # 4. Result folder
    # --------------------------------------------------------

    result_folder = Path(
        result_folder
    )

    result_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # 5. Snapshot старых GeoJSON
    # ДО запуска MIKE
    # --------------------------------------------------------

    previous_files = snapshot_mike_files(
        result_folder
    )

    # --------------------------------------------------------
    # 6. POST /calc_oil_spill
    # --------------------------------------------------------

    logger.info(
        "Sending modeling request to MIKE..."
    )

    logger.info(
        "MIKE API: %s",
        MIKE_API_URL,
    )

    logger.info(
        "Result folder: %s",
        result_folder,
    )

    try:

        response = requests.post(
            f"{MIKE_API_URL}/calc_oil_spill",
            json={
                "source_datetime": time_1km,
                "start_hour": 0,
                "run_hours": 120,
                "time_step_interval": 180,
                "xyz": result_xyz,
                "result_folder": str(result_folder),
            },
            timeout=(
                10,
                120,
            ),
        )

    except requests.exceptions.ConnectTimeout:

        raise RuntimeError(
            "MIKE не отвечает: "
            "таймаут соединения"
        )

    except requests.exceptions.ReadTimeout:

        raise RuntimeError(
            "MIKE не ответил вовремя: "
            "таймаут ответа"
        )

    except requests.exceptions.ConnectionError:

        raise RuntimeError(
            "MIKE недоступен: "
            "нет соединения"
        )

    except requests.exceptions.RequestException as exc:

        raise RuntimeError(
            f"Ошибка запроса к MIKE: {exc}"
        )

    if response.status_code != 200:

        raise RuntimeError(
            "MIKE не принял запрос. "
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

    logger.info(
        "MIKE accepted modeling request: %s",
        response.text,
    )

    # --------------------------------------------------------
    # 7. Ждём 4 GeoJSON
    # --------------------------------------------------------

    mike_files = wait_for_mike_results(
        result_folder=result_folder,
        previous_files=previous_files,
        expected_files=EXPECTED_MIKE_FILES,
        timeout=MIKE_RESULTS_TIMEOUT,
        interval=MIKE_RESULTS_INTERVAL,
    )

    return mike_files


# ============================================================
# SAVE MODELING RESULTS
# ============================================================

def save_modeling_results(
    analysis_id,
    mike_files,
):
    """
    Записать результаты MIKE в oil_spill_modeling_results.

    Один Feature GeoJSON = одна запись БД.

    Примеры:

        *_oil_cells_039.geojson
            result_type = step
            model_step = 39

        *_oil_cells_079.geojson
            result_type = step
            model_step = 79

        *_oil_cells_120.geojson
            result_type = step
            model_step = 120

        *_oil_max_cells_0.geojson
            result_type = max
            model_step = 0
    """

    conn = get_db_connection()

    try:

        conn.autocommit = False

        total_inserted = 0

        with conn.cursor() as cur:

            for filepath in mike_files:

                filepath = Path(filepath)

                filename = filepath.name

                # ------------------------------------------------
                # Определяем тип и шаг
                # ------------------------------------------------

                step_match = re.search(
                    r"_oil_cells_(\d+)\.geojson$",
                    filename,
                    re.IGNORECASE,
                )

                max_match = re.search(
                    r"_oil_max_cells_(\d+)\.geojson$",
                    filename,
                    re.IGNORECASE,
                )

                if step_match:

                    result_type = "step"

                    model_step = int(
                        step_match.group(1)
                    )

                elif max_match:

                    result_type = "max"

                    model_step = int(
                        max_match.group(1)
                    )

                else:

                    logger.warning(
                        "Unknown MIKE result filename, skipped: %s",
                        filename,
                    )

                    continue

                # ------------------------------------------------
                # Читаем GeoJSON
                # ------------------------------------------------

                logger.info(
                    "Reading MIKE GeoJSON: %s",
                    filepath,
                )

                with open(
                    filepath,
                    "r",
                    encoding="utf-8",
                ) as f:

                    geojson = json.load(f)

                if (
                    not isinstance(geojson, dict)
                    or geojson.get("type")
                    != "FeatureCollection"
                ):

                    raise RuntimeError(
                        f"Invalid MIKE GeoJSON: {filepath}"
                    )

                features = geojson.get(
                    "features",
                    [],
                )

                logger.info(
                    "MIKE file %s contains %d polygons "
                    "(result_type=%s, model_step=%d)",
                    filename,
                    len(features),
                    result_type,
                    model_step,
                )

                # ------------------------------------------------
                # Один Feature = одна запись
                # ------------------------------------------------

                file_inserted = 0

                for index, feature in enumerate(
                    features,
                    start=1,
                ):

                    geometry = feature.get(
                        "geometry"
                    )

                    if not geometry:

                        logger.warning(
                            "Feature %d in %s has no geometry, skipped",
                            index,
                            filename,
                        )

                        continue

                    properties = (
                        feature.get("properties")
                        or {}
                    )

                    volatile_m = properties.get(
                        "Volatile_M"
                    )

                    heavy_mass = properties.get(
                        "Heavy_Mass"
                    )

                    wax_mass = properties.get(
                        "Wax_Mass"
                    )

                    asph_mass = properties.get(
                        "Asph_Mass"
                    )

                    oil_total = properties.get(
                        "oil_total"
                    )

                    # ------------------------------------------------
                    # INSERT
                    # ------------------------------------------------

                    cur.execute(
                        """
                        INSERT INTO oil_spill_modeling_results (
                            analysis_id,
                            model_step,
                            result_type,
                            volatile_m,
                            heavy_mass,
                            wax_mass,
                            asph_mass,
                            oil_total,
                            geometry,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            ST_SetSRID(
                                ST_GeomFromGeoJSON(%s),
                                4326
                            ),
                            NOW(),
                            NOW()
                        )
                        """,
                        (
                            analysis_id,
                            model_step,
                            result_type,
                            volatile_m,
                            heavy_mass,
                            wax_mass,
                            asph_mass,
                            oil_total,
                            json.dumps(geometry),
                        ),
                    )

                    file_inserted += 1
                    total_inserted += 1

                logger.info(
                    "Inserted %d polygons from %s",
                    file_inserted,
                    filename,
                )

        conn.commit()

        logger.info(
            "Modeling results saved successfully: "
            "analysis=%s, total polygons=%d",
            analysis_id,
            total_inserted,
        )

        return total_inserted

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# UPDATE SUCCESS STATUS
# ============================================================

def mark_modeling_success(
    analysis_id,
):
    """
    modeling -> modeled
    """

    conn = get_db_connection()

    try:

        conn.autocommit = False

        # ВАЖНО:
        # статус называется modeled
        modeled_status_id = get_status_id(
            conn,
            "modeled",
        )

        modeling_status_id = get_status_id(
            conn,
            "modeling",
        )

        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE oil_spill_analyses
                SET
                    status_id = %s,
                    modeling_finished_at = NOW(),
                    error = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status_id = %s
                """,
                (
                    modeled_status_id,
                    analysis_id,
                    modeling_status_id,
                ),
            )

            if cur.rowcount != 1:

                raise RuntimeError(
                    f"Could not change analysis "
                    f"{analysis_id} "
                    f"from modeling to modeled"
                )

        conn.commit()

        logger.info(
            "Analysis %s status changed to modeled",
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

def mark_modeling_failed(
    analysis_id,
    error,
):
    """
    modeling -> modeling_failed
    """

    conn = get_db_connection()

    try:

        conn.autocommit = False

        failed_status_id = get_status_id(
            conn,
            "modeling_failed",
        )

        modeling_status_id = get_status_id(
            conn,
            "modeling",
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
                    modeling_finished_at = NOW(),
                    error = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND status_id = %s
                """,
                (
                    failed_status_id,
                    error_text,
                    analysis_id,
                    modeling_status_id,
                ),
            )

        conn.commit()

        logger.info(
            "Analysis %s status changed to modeling_failed",
            analysis_id,
        )

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ============================================================
# PROCESS ONE ANALYSIS
# ============================================================

def process_analysis(
    analysis,
    token,
):
    """
    Полный цикл моделирования одной Analysis.
    """

    analysis_id = analysis["id"]

    try:

        # ====================================================
        # 1. DOWNLOAD TIFF
        # ====================================================

        tiff_path = download_tiff(
            analysis,
            token,
        )

        # ====================================================
        # 2. GET DETECTIONS
        # ====================================================

        detections = get_detections(
            analysis_id,
            token,
        )

        # ====================================================
        # 3. ONLY class == 1
        #
        # status НЕ проверяем
        # ====================================================

        class_1_geojson = (
            extract_class_1_geojson(
                detections
            )
        )

        # ====================================================
        # 4. RESULT FOLDER
        # ====================================================

        base_name = tiff_path.stem

        result_folder = (
            Path(RESULTS_DRIVE)
            / base_name
        )

        result_folder.mkdir(
            parents=True,
            exist_ok=True,
        )

        logger.info(
            "Result folder: %s",
            result_folder,
        )

        # ====================================================
        # 5. MASK TIFF
        # ====================================================

        mask_tif = (
            result_folder
            / f"{base_name}_cl.tif"
        )

        # ====================================================
        # 6. 1 KM TIFF
        # ====================================================

        result_1km_tif = (
            result_folder
            / f"{base_name}_1km.tif"
        )

        # ====================================================
        # 7. GEOJSON -> MASK
        # ====================================================

        geojson_to_mask_tif(
            class_1_geojson,
            tiff_path,
            mask_tif,
        )

        # ====================================================
        # 8. MASK -> 1 KM -> XYZ -> MIKE
        # ====================================================

        mike_files = run_model(
            tiff_path,
            mask_tif,
            result_1km_tif,
            result_folder,
        )

        # ====================================================
        # 9. MIKE RESULTS
        # ====================================================

        logger.info(
            "MIKE returned %d GeoJSON files",
            len(mike_files),
        )

        for filepath in mike_files:

            logger.info(
                "MIKE result available: %s",
                filepath,
            )

        # ====================================================
        # 10. SAVE RESULTS TO DATABASE
        # ====================================================
        #
        # Один Feature = одна запись
        #

        inserted_results = save_modeling_results(
            analysis_id=analysis_id,
            mike_files=mike_files,
        )

        logger.info(
            "Saved %d modeling polygons for analysis %s",
            inserted_results,
            analysis_id,
        )

        # ====================================================
        # 11. modeling -> modeled
        # ====================================================

        mark_modeling_success(
            analysis_id,
        )

        logger.info(
            "Modeling completed successfully "
            "for analysis %s",
            analysis_id,
        )

    except Exception as exc:

        logger.exception(
            "Modeling failed for analysis %s",
            analysis_id,
        )

        try:

            mark_modeling_failed(
                analysis_id,
                exc,
            )

        except Exception:

            logger.exception(
                "Could not mark analysis %s "
                "as modeling_failed",
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
        "Oil Spill Modeling Worker"
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
        "MIKE API: %s",
        MIKE_API_URL,
    )

    logger.info(
        "Laravel API: %s",
        LARAVEL_API_URL,
    )

    logger.info(
        "Expected MIKE GeoJSON files: %d",
        EXPECTED_MIKE_FILES,
    )

    token = None

    while True:

        try:

            # =================================================
            # 1. CLAIM
            # =================================================

            analysis = claim_analysis()

            if analysis is None:

                time.sleep(
                    POLL_INTERVAL
                )

                continue

            # =================================================
            # 2. LOGIN
            # =================================================

            if token is None:

                token = login_laravel()

            # =================================================
            # 3. PROCESS
            # =================================================

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
                "Modeling Worker stopped"
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