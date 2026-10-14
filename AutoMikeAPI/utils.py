import os
import time
import requests
import shutil
import subprocess
import shutil
import datetime
import logging
import mikeio
from grib2dfs import convert_grib_to_dfs, convert_grib_to_dfs_history
from source import create_source
from mzt import run_mzt
from urllib.request import build_opener




# DATA_DIR = "C:/Users/osuser/AutoMikeAPI/data"
DATA_DIR  = "Z:/mike_data"
API_DIR = 'C:/Users/osuser/AutoMikeAPI'
ICBC_DIR = os.path.join(DATA_DIR, "icbc")
SALINITY_DIR = os.path.join(DATA_DIR, "salinity")
DFS_DIR = os.path.join(DATA_DIR, "dfs")
MIKE_BIN_DIR = "C:/Program Files (x86)/DHI/2014/bin/x64"



# Download salinity
def download_salinity(year, month, day, hour, run_hours):
    os.makedirs(SALINITY_DIR, exist_ok=True)

    start_datetime = datetime.datetime(year, month, day, hour)
    end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
    command = ["podaac-data-subscriber", 
               "-c", "SMAP_JPL_L3_SSS_CAP_8DAY-RUNNINGMEAN_V5",
               "-d", SALINITY_DIR,
               "-sd", start_datetime.strftime('%Y-%m-%dT%H:%M:%SZ'), 
               "-ed", end_datetime.strftime('%Y-%m-%dT%H:%M:%SZ')
               ]
    try:
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except:
        logging.warning("Error downloading salinity")

def check_oper_icbc(year, month, day, hour, start_hour, run_hours):    
    grib_dir = os.path.join(ICBC_DIR, f"{year}{month:02}{day:02}{hour:02}")
    for i in range(start_hour, start_hour+run_hours+1):
        filename = f"gfs.t{hour:02}z.pgrb2.0p25.f{i:03}"
        filepath = os.path.join(grib_dir, filename)
        if not os.path.isfile(filepath):
            return False
    return True

# Download GFS
def download(year, month, day, hour, start_hour, run_hours):    
    grib_dir = os.path.join(ICBC_DIR, f"{year}{month:02}{day:02}{hour:02}")
    os.makedirs(ICBC_DIR, exist_ok=True)
    os.makedirs(grib_dir, exist_ok=True)
    url_dir = f"/gfs.{year}{month:02}{day:02}/{hour:02}/atmos"
    url_template = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?file={0}\
&lev_10_m_above_ground=on&lev_2_m_above_ground=on&lev_surface=on\
&var_DSWRF=on&var_TMP=on&var_PRATE=on&var_PRES=on&var_UGRD=on&var_VGRD=on&var_LAND=on\
&subregion=&leftlon=45&rightlon=57&toplat=49&bottomlat=36\
&dir={1}"
    for i in range(start_hour, start_hour+run_hours+1):
        filename = f"gfs.t{hour:02}z.pgrb2.0p25.f{i:03}"
        url = url_template.format(filename, url_dir)
        filepath = os.path.join(grib_dir, filename)
        if not os.path.isfile(filepath):
            for i in range(3):
                try:
                    with requests.get(url, stream=True) as r:
                        with open(filepath, 'wb') as f:
                            shutil.copyfileobj(r.raw, f)
                    break
                except:
                    time.sleep(30)


 # Download GFS
def download_history(year, month, day, hour, start_hour, run_hours):    
    grib_dir = os.path.join(ICBC_DIR, f"{year}{month:02}{day:02}{hour:02}")
    os.makedirs(ICBC_DIR, exist_ok=True)
    os.makedirs(grib_dir, exist_ok=True)
    # url_dir = f"https://data.rda.ucar.edu/ds083.3/{year}/{year}{month:0>2}/"
    # url_dir = f"https://osdf1.amst.nrp.internet2.edu:8443/ncar/gdex/d083003/{year}/{year}{month:0>2}/"
    url_dir = f"https://osdf-data.gdex.ucar.edu/ncar/gdex/d083003/{year}/{year}{month:0>2}/"
    dt = datetime.datetime(year, month, day, hour)
    opener = build_opener()
    for h in range(start_hour, start_hour+run_hours+1, 3):
        fh = h % 6
        filename = f"gdas1.fnl0p25.{dt.year}{dt.month:0>2}{dt.day:0>2}{dt.hour:0>2}.f{fh:02}.grib2"
        url = url_dir+filename
        filepath = os.path.join(grib_dir, filename)
        if not os.path.isfile(filepath):
            for i in range(3):
                try:
                    # with requests.get(url, stream=True) as r:
                    #     with open(filepath, 'wb') as f:
                    #         shutil.copyfileobj(r.raw, f)
                    # ofile = os.path.basename(filepath)
                    infile = opener.open(url)
                    outfile = open(filepath, "wb")
                    outfile.write(infile.read())
                    outfile.close()
                    break
                except:
                    time.sleep(30)
        if fh == 3:
            dt += datetime.timedelta(hours=6)


def grib_to_dfs(year, month, day, hour, start_hour, run_hours, source=None, history=False):
    grib_dir = os.path.join(ICBC_DIR, f"{year}{month:02}{day:02}{hour:02}")
    if source:
        dfs_date_dir = os.path.join(DFS_DIR, datetime.datetime.strptime(source.source_datetime, '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S'))
    else:
        dfs_date_dir = os.path.join(DFS_DIR, f"{year}{month:02}{day:02}{hour:02}")
    os.makedirs(DFS_DIR, exist_ok=True)
    os.makedirs(dfs_date_dir, exist_ok=True)
    start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
    end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
    meteo_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo.dfs2")
    tp_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_tp.dfs2")
    ssrd_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_ssrd.dfs0")
    if (os.path.isfile(meteo_file_path) and os.path.isfile(tp_file_path) and os.path.isfile(ssrd_file_path)): 
        return
    if history:
        convert_grib_to_dfs_history(grib_dir, dfs_date_dir, start_hour, start_datetime, run_hours)
    else:
        convert_grib_to_dfs(grib_dir, dfs_date_dir, hour, start_hour, run_hours)


def prepare_hd_configs(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, radius=150):
    dfs_date_dir = os.path.join(DFS_DIR, datetime.datetime.strptime(source.source_datetime, '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S'))
    # edit hydro.m21
    with open(os.path.join(API_DIR, 'hydro_template.m21'), 'r') as f:
        data = f.read()
        start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
        end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
        data = data.replace('<start_time_tmp>',  f"{start_datetime.year}, {start_datetime.month}, {start_datetime.day}, {start_datetime.hour}, 0, 0")
        data = data.replace('<number_of_timesteps_tmp>', str(number_of_timesteps))
        data = data.replace('<time_step_interval_tmp>', str(time_step_interval))
        meteo_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo.dfs2")
        data = data.replace('<meteo_file_path_tmp>', meteo_file_path)
        tp_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_tp.dfs2")
        data = data.replace('<tp_file_path_tmp>', tp_file_path)
        e_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_e.dfs2")
        data = data.replace('<e_file_path_tmp>', e_file_path)
        dfs_level_dir = os.path.join(source.result_folder, 'mike/dfs')
        os.makedirs(dfs_level_dir, exist_ok=True)
        output_file_path = os.path.join(dfs_level_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_level.dfs2")
        data = data.replace('<output_file_path_tmp>', output_file_path)

        # Output area
        dfs = mikeio.read(meteo_file_path)
        # Extract the geographical properties of the grid
        x0, y0 = dfs.geometry.origin      # Origin coordinates (lower-left corner)
        dx, dy = dfs.geometry.dx, dfs.geometry.dy  # Grid spacing in the x and y direction
        lon, lat, _ = source.xyz[0]
        # Convert coordinates (lon, lat) to grid indices (i, j)
        i = int((lon - x0) / dx)
        j = int((lat - y0) / dy)
        left, right  = max(0, i - radius), min(dfs.shape[2], i + radius) 
        bottom, top = max(0, j - radius), min(dfs.shape[1], j + radius) 
        data = data.replace('<x_range_tmp>', f'{left}, {right-1}')
        data = data.replace('<y_range_tmp>', f'{bottom}, {top-1}')

        with open(os.path.join(API_DIR, 'hydro.m21'), 'w') as f:
            f.write(data)


def calc_hd(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source=None):
    # start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
    # end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
    # level_file_path = os.path.join(DFS_DIR, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_level.dfs2")
    # if os.path.isfile(level_file_path):
    #     return True 
    
    prepare_hd_configs(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source)

    command = [os.path.join(MIKE_BIN_DIR, "nmodel.exe"), "//apv", os.path.join(API_DIR, 'hydro.m21')]
    p = subprocess.Popen(command, shell=True)
    p.wait()

    with open(os.path.join(API_DIR, 'hydro.log'), 'r') as f:
        data = f.read()
        if "Normal run completion" in data[-50:]:
            return True
    
    return False


def crop_dfs2(dfs_date_dir, dfs_out_dir, year, month, day, hour, start_hour, run_hours, source, radius=150):
    start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
    end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
    meteo_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo.dfs2")
    dfs = mikeio.read(meteo_file_path)

    # Extract the geographical properties of the grid
    x0, y0 = dfs.geometry.origin      # Origin coordinates (lower-left corner)
    dx, dy = dfs.geometry.dx, dfs.geometry.dy  # Grid spacing in the x and y direction
    lon, lat, _ = source.xyz[0]
    # Convert coordinates (lon, lat) to grid indices (i, j)
    i = int((lon - x0) / dx)
    j = int((lat - y0) / dy)
    left, right  = max(0, i - radius), min(dfs.shape[2], i + radius) 
    bottom, top = max(0, j - radius), min(dfs.shape[1], j + radius) 

    # Crop meteo file
    dfs_crop = dfs.isel(x=slice(left, right), y=slice(bottom, top))
    meteo_crop_file_path = os.path.join(dfs_out_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo_crop.dfs2")
    dfs_crop.to_dfs(meteo_crop_file_path)  


def prepare_os_configs(dfs_date_dir, dfs_out_dir, year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source_text, crop_dfs, source):
    # edit oil.noil
    with open(os.path.join(API_DIR, 'oil_template.noil'), 'r') as f:
        data = f.read()
        start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
        end_datetime = start_datetime + datetime.timedelta(hours=run_hours)
        data = data.replace('<start_time_tmp>',  f"{start_datetime.year}, {start_datetime.month}, {start_datetime.day}, {start_datetime.hour}, 0, 0")
        data = data.replace('<number_of_timesteps_tmp>', str(number_of_timesteps))
        data = data.replace('<time_step_interval_tmp>', str(time_step_interval))
        level_file_path = os.path.join(dfs_out_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_level.dfs2")
        data = data.replace('<level_file_path_tmp>', level_file_path)
        if crop_dfs:
            meteo_file_path = os.path.join(dfs_out_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo_crop.dfs2")
        else:
            meteo_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_meteo.dfs2")
        data = data.replace('<meteo_file_path_tmp>', meteo_file_path)
        ssrd_file_path = os.path.join(dfs_date_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_ssrd.dfs0")
        data = data.replace('<ssrd_file_path_tmp>', ssrd_file_path)
        data = data.replace('<sources_tmp>', source_text)

        output_file_path = os.path.join(dfs_out_dir, f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_oil.dfs2")
        data = data.replace('<output_file_path_tmp>', output_file_path)
        with open(os.path.join(API_DIR, 'oil.noil'), 'w') as f:
            f.write(data)

        return output_file_path


def calc_os(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, crop_dfs=True):
    dfs_out_dir = os.path.join(source.result_folder, 'mike/dfs')
    os.makedirs(dfs_out_dir, exist_ok=True)
    dfs_date_dir = os.path.join(DFS_DIR, datetime.datetime.strptime(source.source_datetime, '%Y-%m-%d %H:%M:%S').strftime('%Y%m%d%H%M%S'))
    # prepare configs
    start_datetime = datetime.datetime(year, month, day, hour) + datetime.timedelta(hours=start_hour)
    source_text = create_source(dfs_out_dir, start_datetime, run_hours, time_step_interval, source)
    
    if crop_dfs:
        crop_dfs2(dfs_date_dir, dfs_out_dir, year, month, day, hour, start_hour, run_hours, source)

    oil_file_path = prepare_os_configs(dfs_date_dir, dfs_out_dir, year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source_text, crop_dfs, source)

    # if os.path.isfile(oil_file_path):
    #     run_mzt(MIKE_BIN_DIR, API_DIR, oil_file_path)
    #     return True

    command = [os.path.join(MIKE_BIN_DIR, "m321_os.exe"), "//apv", os.path.join(API_DIR, 'oil.noil')]
    p = subprocess.Popen(command, shell=True)
    p.wait()

    with open(os.path.join(API_DIR, 'oil.log'), 'r') as f:
        data = f.read()
        if "Normal run completion" in data[-50:]:
            run_mzt(MIKE_BIN_DIR, API_DIR, oil_file_path)
            return True
    
    return False


