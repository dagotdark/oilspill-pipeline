import os
import json
import time
import rasterio
from rasterio.features import rasterize
from osgeo import ogr, osr
import requests

# Импортируем существующие функции
from main import aggregate_to_km_resolution, tiff_to_xyz, extract_timestamp_from_filename, RESULTS_DRIVE


SRC_TIF = r"test_data\S1A_20240802T025150.tif"
GEOJSON = r"test_data\S1A_20240802T025150_filtered.geojson"

RESULT_DIR = "test_data"

MASK_TIF = os.path.join(
    RESULT_DIR,
    "S1A_20240802T025150_cl.tif"
)

RESULT_1KM_TIF = os.path.join(
    RESULT_DIR,
    "S1A_20240802T025150_1km.tif"
)


def geojson_to_mask_tif(geojson_path, src_tif, output_tif):

    with open(geojson_path, "r", encoding="utf-8") as f:
        geojson = json.load(f)

    # EPSG:4326 -> EPSG:32639
    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(4326)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(32639)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = osr.CoordinateTransformation(src_srs, dst_srs)

    shapes = []

    for feature in geojson["features"]:

        geometry = feature.get("geometry")

        if geometry:
            ogr_geometry = ogr.CreateGeometryFromJson(
                json.dumps(geometry)
            )

            # Трансформируем геометрию в CRS исходного TIFF
            ogr_geometry.Transform(transform)

            geometry = json.loads(
                ogr_geometry.ExportToJson()
            )

            shapes.append((geometry, 1))

    if not shapes:
        raise ValueError("В GeoJSON нет геометрий")

    with rasterio.open(src_tif) as src:

        profile = src.profile.copy()

        profile.update(
            dtype="uint8",
            count=1,
            compress="lzw"
        )

        mask = rasterize(
            shapes=shapes,
            out_shape=(src.height, src.width),
            transform=src.transform,
            fill=0,
            dtype="uint8"
        )

        with rasterio.open(output_tif, "w", **profile) as dst:
            dst.write(mask, 1)

    print(f"Маска создана: {output_tif}")


def wait_for_mike_results(result_folder, expected_files=4,
                          timeout=3600, interval=10):

    geojson_dir = os.path.join(
        result_folder,
        "mike",
        "geojson"
    )

    print()
    print("Ожидаем результаты MIKE:")
    print(geojson_dir)

    start_time = time.time()

    while True:

        elapsed = int(time.time() - start_time)

        if os.path.isdir(geojson_dir):

            geojson_files = [
                os.path.join(geojson_dir, filename)
                for filename in os.listdir(geojson_dir)
                if filename.lower().endswith(".geojson")
            ]

            print(
                f"Прошло {elapsed} сек. "
                f"Найдено GeoJSON: {len(geojson_files)}/{expected_files}"
            )

            if len(geojson_files) >= expected_files:

                print()
                print("MIKE завершил работу.")
                print("Найдены файлы:")

                for filepath in geojson_files:
                    print(filepath)

                return geojson_files

        else:

            print(
                f"Прошло {elapsed} сек. "
                f"Папка результатов пока отсутствует"
            )

        if elapsed >= timeout:
            raise TimeoutError(
                f"MIKE не создал {expected_files} GeoJSON "
                f"за {timeout} секунд"
            )

        time.sleep(interval)


def main():

    os.makedirs(RESULT_DIR, exist_ok=True)

    # 1. GeoJSON -> TIFF 0/1
    geojson_to_mask_tif(
        GEOJSON,
        SRC_TIF,
        MASK_TIF
    )

    # 2. Используем СТАРУЮ функцию
    aggregate_to_km_resolution(
        MASK_TIF,
        RESULT_1KM_TIF
    )

    print(f"1 km raster создан: {RESULT_1KM_TIF}")

    # 3. Используем СТАРУЮ функцию
    
    xyz = tiff_to_xyz(RESULT_1KM_TIF)
    result_xyz  = list([list(x) for x in xyz])
    time_1km = extract_timestamp_from_filename(RESULT_1KM_TIF)

    print()
    print("XYZ:")
    print(xyz)

    print()
    print("Количество точек:", len(xyz))

    
    # Дата из имени исходного TIFF
    # S1A_20240802T025150
    base_name = os.path.basename(SRC_TIF)[:-4]

    

    # Папка результатов на Z
    result_folder = os.path.join(
        RESULTS_DRIVE,
        base_name
    )

    print()
    print("Result folder:")
    print(result_folder)

    if xyz.size > 0:

        print()
        print("Отправляется запрос...")

        print("Date:", time_1km)

        try:

            response = requests.post(
                'http://127.0.0.1:8000/calc_oil_spill',

                json={
                    "source_datetime": time_1km,
                    "start_hour": 0,
                    "run_hours": 120,
                    "time_step_interval": 180,
                    "xyz": result_xyz,
                    "result_folder": result_folder
                },

                timeout=(10, 120)
            )

            if response.status_code == 200:

                msg = {
                    'status': response.status_code,
                    'msg': 'Успешно отправлен запрос на майк'
                }

                print(msg)

                # -------------------------------------------------
                # Ждем результаты MIKE
                # -------------------------------------------------

                mike_files = wait_for_mike_results(
                    result_folder,
                    expected_files=4,
                    timeout=3600,
                    interval=10
                )

                print()
                print("Получены результаты MIKE:")

                for filepath in mike_files:
                    print(filepath)

            else:

                print(response)

                msg = {
                    'status': response.status_code,
                    'msg': 'Ошибка, майк не принял запрос'
                }

        except requests.exceptions.ConnectTimeout:

            msg = {
                'status': 504,
                'msg': 'Майк не отвечает: таймаут соединения'
            }

            print(msg)

        except requests.exceptions.ReadTimeout:

            msg = {
                'status': 504,
                'msg': 'Майк не ответил вовремя: таймаут ответа'
            }

            print(msg)

        except requests.exceptions.ConnectionError:

            msg = {
                'status': 503,
                'msg': 'Майк недоступен: нет соединения'
            }

            print(msg)

        except requests.exceptions.RequestException as e:

            msg = {
                'status': 500,
                'msg': f'Ошибка запроса к майку: {e}'
            }

            print(msg)

        except TimeoutError as e:

            msg = {
                'status': 504,
                'msg': str(e)
            }

            print(msg)

    else:

        msg = {
            'status': 500,
            'msg': 'Модель не нашла разлив нефти'
        }

    print()
    print(msg)


print("TEST.PY START")


if __name__ == "__main__":

    print("MAIN START")

    main()