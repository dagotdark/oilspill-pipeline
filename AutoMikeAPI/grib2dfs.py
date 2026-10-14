import os
import datetime
import xarray as xr
import pandas as pd
import numpy as np
from mikeio import Dataset, Grid2D
from mikeio.eum import EUMType, EUMUnit, ItemInfo
from scipy import interpolate
from scipy.interpolate import griddata
from pyproj import Transformer



def interp_data(data):
    indexes = np.arange(data.shape[0])
    good = np.isfinite(data).all(axis=(1, 2)) 
    f = interpolate.interp1d(indexes[good], data[good],
                            bounds_error=False,
                            axis=0)
    B = f(indexes)
    return B


def _read_ssrd(ds):
    # Нисходящая коротковолновая радиация у поверхности: в новых файлах
    # короткое имя 'sdswrf', в старых — 'dswrf'. Берём то, что есть.
    for name in ('sdswrf', 'dswrf'):
        if name in ds:
            return ds[name].values
    raise KeyError("Не найдено ни 'sdswrf', ни 'dswrf' в наборе данных")


def read_grib(grib_file, history=False):
    # Open the GRIB file using xarray
    ds_sfc = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'surface', 'stepType': 'instant'})
    ds_hag_10m = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'heightAboveGround', 'level': 10})
    ds_hag_2m = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'heightAboveGround', 'level': 2})

    if history:
        # Clip an xarray to a smaller extent given the lat/lon coordinates
        min_lon = 45 
        min_lat = 36 
        max_lon = 57 
        max_lat = 49
        ds_sfc = ds_sfc.sel(latitude=slice(max_lat, min_lat), longitude=slice(min_lon, max_lon))
        ds_hag_10m = ds_hag_10m.sel(latitude=slice(max_lat, min_lat), longitude=slice(min_lon, max_lon))
        ds_hag_2m = ds_hag_2m.sel(latitude=slice(max_lat, min_lat), longitude=slice(min_lon, max_lon))

        # Reverse an array along one coordinate 
        ds_sfc = ds_sfc.reindex(latitude=ds_sfc.latitude[::-1])
        ds_hag_10m = ds_hag_10m.reindex(latitude=ds_hag_10m.latitude[::-1])
        ds_hag_2m = ds_hag_2m.reindex(latitude=ds_hag_2m.latitude[::-1])
        
    # Extract data variables and coordinates
    times = pd.to_datetime(ds_sfc['time'].values)
    lat = ds_sfc['latitude'].values
    lon = ds_sfc['longitude'].values

    u_10 = ds_hag_10m['u10'].values
    v_10 = ds_hag_10m['v10'].values
    sp = ds_sfc['sp'].values / 100.0 # hPa
    t2 = ds_hag_2m['t2m'].values - 273.15 # Celsium
    t_sfc = ds_sfc['t'].values - 273.15 # Celsium
    if history:
        tp = np.full([len(lat), len(lon)], np.nan)
    else:
        tp = ds_sfc['prate'].values * 24.0 * 3600.0 # mm/day
    ssrd = np.full([len(lat), len(lon)], np.nan)

    ds_sfc.close()
    ds_hag_10m.close()
    ds_hag_2m.close()

    if history:
        if 'f00' not in grib_file:
            ds_sfc_acc = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'surface', 'stepType': 'accum'})
            ds_sfc_avg = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'surface', 'stepType': 'avg'})
            ds_sfc_acc = ds_sfc_acc.sel(latitude=slice(max_lat, min_lat), longitude=slice(min_lon, max_lon))
            ds_sfc_avg = ds_sfc_avg.sel(latitude=slice(max_lat, min_lat), longitude=slice(min_lon, max_lon))
            tp = ds_sfc_acc['tp'].values
            ssrd = _read_ssrd(ds_sfc_avg)
            ds_sfc_acc.close()
            ds_sfc_avg.close()
    else:
        if 'f000' not in grib_file:
            ds_sfc_avg = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'surface', 'stepType': 'avg'})
            ssrd = _read_ssrd(ds_sfc_avg)
            ds_sfc_avg.close()

    data = np.array([u_10, v_10, sp, t2, t_sfc, tp, ssrd])

    return lat, lon, times, data


# -------------------------------------------------------------------
# Целевая сетка MIKE (UTM Zone 39N).
# Значения соответствуют прежней обрезанной сетке из GDAL-репроекции
# (nx=721, ny=1291, шаг 1000 м, origin в левом нижнем углу).
# -------------------------------------------------------------------
DX = 1000.0
NX = 721
NY = 1291
X0 = 106859.769432844
Y0 = 4039660.29587709

# Трансформер WGS84 (EPSG:4326) -> UTM Zone 39N (EPSG:32639)
TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:32639", always_xy=True)

# Координаты узлов целевой сетки (в метрах, UTM)
_X_COORDS = np.arange(NX) * DX + X0
_Y_COORDS = np.arange(NY) * DX + Y0
GRID_X, GRID_Y = np.meshgrid(_X_COORDS, _Y_COORDS)


def interp_to_grid(src_points, values):
    """Интерполяция значений из точек src_points на целевую сетку MIKE.

    Линейная интерполяция даёт гладкие поля, а края и пропуски
    (вне выпуклой оболочки исходных точек) заполняются методом
    ближайшего соседа.
    """
    # 1. Линейная интерполяция (гладкие поля)
    grid = griddata(src_points, values, (GRID_X, GRID_Y), method='linear')

    # 2. Заполнение NaN (края/пропуски) методом ближайшего соседа
    nan_mask = np.isnan(grid)
    if nan_mask.any():
        grid_nearest = griddata(src_points, values, (GRID_X, GRID_Y), method='nearest')
        grid[nan_mask] = grid_nearest[nan_mask]

    # 3. Финальная страховка от системных пропусков
    if np.isnan(grid).any():
        mean = np.nanmean(grid)
        grid = np.nan_to_num(grid, nan=(mean if not np.isnan(mean) else 0.0))

    return grid


def data_to_dfs_utm(dfs_dir, lat, lon, times_list, data_array, history=False):

    # Координаты исходной сетки GFS (WGS84) в проекции UTM Zone 39N.
    # Точки постоянны для всех параметров и моментов времени, поэтому
    # трансформируем их один раз.
    lon2d, lat2d = np.meshgrid(lon, lat)
    src_x, src_y = TRANSFORMER.transform(lon2d, lat2d)
    src_points = np.column_stack((src_x.ravel(), src_y.ravel()))

    # Интерполяция каждого параметра на каждый момент времени на целевую сетку
    n_items, n_times = data_array.shape[0], data_array.shape[1]
    reprojected_data = np.empty((n_items, n_times, NY, NX), dtype=np.float32)
    for b in range(n_items):
        for t in range(n_times):
            reprojected_data[b, t] = interp_to_grid(src_points, data_array[b, t].ravel())

    # Целевая сетка MIKE (UTM Zone 39N)
    grid = Grid2D(nx=NX, ny=NY, dx=DX, dy=DX, x0=X0, y0=Y0, projection="UTM-39")
   
    # Create Meteo
    meteo_items = [
        ItemInfo(name="10U", itemtype=EUMType.Wind_speed, unit=EUMUnit.meter_per_sec),
        ItemInfo(name="10V", itemtype=EUMType.Wind_speed, unit=EUMUnit.meter_per_sec),
        ItemInfo(name="sp", itemtype=EUMType.Pressure, unit=EUMUnit.hectopascal),
        ItemInfo(name='2t', itemtype=EUMType.Temperature, unit=EUMUnit.degree_Celsius),
        ItemInfo(name='t_sfc', itemtype=EUMType.Temperature, unit=EUMUnit.degree_Celsius),
        
    ]  
    meteo_data = reprojected_data[:5]
    meteo_dfs2 = Dataset(meteo_data, time=times_list, items=meteo_items, geometry=grid)
    meteo_dfs2_file = f"{times_list[0].strftime('%Y%m%d%H')}_{times_list[-1].strftime('%Y%m%d%H')}_meteo.dfs2"
    meteo_dfs2_file_path = os.path.join(dfs_dir, meteo_dfs2_file)
    if not os.path.isfile(meteo_dfs2_file_path):
        meteo_dfs2.to_dfs(meteo_dfs2_file_path)
        print(f"GRIB file has been converted to DFS2 file {meteo_dfs2_file}")

    # Create TP
    tp_items = [
        ItemInfo(name="tp", itemtype=EUMType.Precipitation_Rate, unit=EUMUnit.mm_per_day)
    ]  

    tp_data = reprojected_data[5]
    if history:
        tp_data = tp_data * 24.0 * 3600.0 / (times_list[1] - times_list[0]).total_seconds()
    tp_data = np.expand_dims(tp_data, axis=0)
    tp_dfs2 = Dataset(tp_data, time=times_list, items=tp_items, geometry=grid)
    # Write the dataset to a DFS2 file
    tp_dfs2_file = f"{times_list[0].strftime('%Y%m%d%H')}_{times_list[-1].strftime('%Y%m%d%H')}_tp.dfs2"
    tp_dfs2_file_path = os.path.join(dfs_dir, tp_dfs2_file)
    if not os.path.isfile(tp_dfs2_file_path):
        tp_dfs2.to_dfs(tp_dfs2_file_path)
        print(f"GRIB file has been converted to DFS2 file {tp_dfs2_file}")

    # Create SSRD
    ssrd_items = [
        ItemInfo(name='ssrd', itemtype=EUMType.Radiation_intensity, unit=EUMUnit.watt_per_meter_pow_2)
    ]
    ssrd_data = np.expand_dims(np.average(reprojected_data[6], axis=(1,2)), axis=0)
    ssrd_dfs0 = Dataset(ssrd_data, time=times_list, items=ssrd_items)
    # Write the dataset to a DFS0 file
    ssrd_dfs0_file = f"{times_list[0].strftime('%Y%m%d%H')}_{times_list[-1].strftime('%Y%m%d%H')}_ssrd.dfs0"
    ssrd_dfs0_file_path = os.path.join(dfs_dir, ssrd_dfs0_file)
    if not os.path.isfile(ssrd_dfs0_file_path):
        ssrd_dfs0.to_dfs(ssrd_dfs0_file_path)
        print(f"GRIB file has been converted to DFS0 file {ssrd_dfs0_file}")


def convert_grib_to_dfs(grib_dir, dfs_dir, hour, start_hour, run_hours):
    times_list = []
    data_list = []
    for i in range(start_hour, start_hour+run_hours+1):
        filename = f"gfs.t{hour:02}z.pgrb2.0p25.f{i:03}"
        grib_file = os.path.join(grib_dir, filename)
        print(grib_file)
        lat, lon, times, data = read_grib(grib_file)
        times_list.append(times + datetime.timedelta(hours=i))
        data_list.append(data)

    data_array = np.array(data_list).swapaxes(0, 1)
    data_array[6] = np.nan_to_num(interp_data(data_array[6]), 0)
    data_to_dfs_utm(dfs_dir, lat, lon, times_list, data_array)


def convert_grib_to_dfs_history(grib_dir, dfs_dir, start_hour, dt, run_hours):
    times_list = []
    data_list = []
    for i in range(start_hour, start_hour+run_hours+1, 3):
        fh = i % 6
        filename = f"gdas1.fnl0p25.{dt.year}{dt.month:0>2}{dt.day:0>2}{dt.hour:0>2}.f{fh:02}.grib2"
        grib_file = os.path.join(grib_dir, filename)
        print(grib_file)
        lat, lon, times, data = read_grib(grib_file, history=True)
        if 'f03' in grib_file:
            times_list.append(times+datetime.timedelta(hours=3))
        else:
            times_list.append(times)
        data_list.append(data)
        if fh == 3:
            dt += datetime.timedelta(hours=6)

    data_array = np.array(data_list).swapaxes(0, 1)
    data_array[5] = np.nan_to_num(interp_data(data_array[5]), 0)
    data_array[6] = np.nan_to_num(interp_data(data_array[6]), 0)
    data_to_dfs_utm(dfs_dir, lat, lon, times_list, data_array, history=True)


