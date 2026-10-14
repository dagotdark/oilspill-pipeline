import os
import mikeio
import logging
import subprocess
import geopandas as gpd



def edit_mzt_file(mzt_file_path, oil_file_path, number_of_timesteps, out_file_path, x_max, y_max):

    with open(os.path.join(os.path.dirname(mzt_file_path), f"template_{os.path.basename(mzt_file_path)}"), 'r') as f:
        data = f.read()
        data = data.replace('<input_file_path_tmp>',  oil_file_path)
        data = data.replace('<last_timestep_tmp>', str(number_of_timesteps-1))
        data = data.replace('<x_max_tmp>',  str(x_max))
        data = data.replace('<y_max_tmp>',  str(y_max))
        data = data.replace('<time_steps_tmp>',  f'{number_of_timesteps//3-1}, {2*number_of_timesteps//3-1}, {number_of_timesteps-1}')
        data = data.replace('<output_file_path_tmp>', out_file_path)
        with open(mzt_file_path, 'w') as f:
            f.write(data)


def add_oil_total(dfs_file_path):
    """Insert an 'oil_total' item (sum of the 4 oil fractions) as item 5 in a dfs2 file.

    The oil output dfs2 holds the four oil-mass fractions as the first four items
    (Volatile_Mass, Heavy_Mass, Wax_Mass, Asph_Mass). Their sum is added as a new
    'oil_total' field so it propagates to the shp and geojson exports.
    """
    ds = mikeio.read(dfs_file_path)
    if len(ds) < 4 or 'oil_total' in [it.name for it in ds.items]:
        return
    oil_total = ds[0] + ds[1] + ds[2] + ds[3]
    oil_total.item.name = 'oil_total'
    oil_total.item.type = ds[0].item.type
    oil_total.item.unit = ds[0].item.unit
    items = [ds[0], ds[1], ds[2], ds[3], oil_total] + [ds[i] for i in range(4, len(ds))]
    mikeio.Dataset(items).to_dfs(dfs_file_path)


def shp2geojson(file_path, max=False):
    gdf = gpd.read_file(file_path)
    gdf = gdf.to_crs("EPSG:4326")
    if max:
        gdf.columns = ['Volatile_M', 'Heavy_Mass', 'Wax_Mass', 'Asph_Mass', 'oil_total', 'geometry']
        gdf = gdf.drop(columns=['Volatile_M', 'Heavy_Mass', 'Wax_Mass', 'Asph_Mass'])
        gdf = gdf[gdf["oil_total"] > 0]
    else:
        gdf = gdf[gdf["Heavy_Mass"] > 0]
        gdf["oil_total"] = (gdf["Volatile_M"] + gdf["Heavy_Mass"] + gdf["Wax_Mass"] + gdf["Asph_Mass"])
    gdf.to_file(file_path.replace('shp', 'geojson'), driver="GeoJSON")


def run_mzt(MIKE_BIN_DIR, API_DIR, oil_file_path):

    # Add oil_total (sum of the 4 oil fractions) as item 5 before exporting
    add_oil_total(oil_file_path)

    dfs = mikeio.read(oil_file_path)
    number_of_timesteps = dfs.shape[0]
    x_max, y_max = dfs.shape[2] - 1, dfs.shape[1] - 1

    # oil2shp
    try:
        mzt_file_path = os.path.join(API_DIR, 'oil2shp.mzt')
        oil_shp_dir_path = os.path.join(os.path.dirname(oil_file_path), '../shp')
        os.makedirs(oil_shp_dir_path, exist_ok=True)
        oil_shp_file_path = os.path.join(oil_shp_dir_path, os.path.basename(oil_file_path).replace('.dfs2', '.shp'))
        edit_mzt_file(mzt_file_path, oil_file_path, number_of_timesteps, oil_shp_file_path, x_max, y_max)
        command = [os.path.join(MIKE_BIN_DIR, "ToolboxShell.exe"), "-run", mzt_file_path, "-x"]
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except Exception as e:
        logging.error(f"Error: oil2shp.mzt {e}")

    # shp to geojson
    try:
        os.makedirs(oil_shp_dir_path.replace('shp', 'geojson'), exist_ok=True)
        digit_count = len(str(abs(number_of_timesteps)))

        # first
        oil_shp_file_path_ = f"{os.path.splitext(oil_shp_file_path)[0]}_cells_{str(number_of_timesteps//3-1).zfill(digit_count)}.shp"
        shp2geojson(oil_shp_file_path_)

        # second
        oil_shp_file_path_ = f"{os.path.splitext(oil_shp_file_path)[0]}_cells_{str(2*number_of_timesteps//3-1).zfill(digit_count)}.shp"
        shp2geojson(oil_shp_file_path_)

        # third
        oil_shp_file_path_ = f"{os.path.splitext(oil_shp_file_path)[0]}_cells_{str(number_of_timesteps-1).zfill(digit_count)}.shp"
        shp2geojson(oil_shp_file_path_)
    except Exception as e:
        logging.error(f"Error: shp2geojson {e}")


    # oil2txt
    try:
        mzt_file_path = os.path.join(API_DIR, 'oil2txt.mzt')
        oil_txt_dir_path = os.path.join(os.path.dirname(oil_file_path), '../txt')
        os.makedirs(oil_txt_dir_path, exist_ok=True)
        oil_txt_file_path = os.path.join(oil_txt_dir_path, os.path.basename(oil_file_path).replace('.dfs2', '.txt'))
        edit_mzt_file(mzt_file_path, oil_file_path, number_of_timesteps, oil_txt_file_path, x_max, y_max)
        command = [os.path.join(MIKE_BIN_DIR, "ToolboxShell.exe"), "-run", mzt_file_path, "-x"]
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except Exception as e:
        logging.error(f"Error: oil2txt.mzt {e}")


    # max_creation
    try:
        mzt_file_path = os.path.join(API_DIR, 'max_creation.mzt')
        max_file_path = oil_file_path.replace('_oil.dfs2', '_oil_max.dfs2')
        edit_mzt_file(mzt_file_path, oil_file_path, number_of_timesteps, max_file_path, x_max, y_max)
        command = [os.path.join(MIKE_BIN_DIR, "ToolboxShell.exe"), "-run", mzt_file_path, "-x"]
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except Exception as e:
        logging.error(f"Error: max_creation.mzt {e}")
    

    # max2shp
    try:
        mzt_file_path = os.path.join(API_DIR, 'max2shp.mzt')
        max_shp_dir_path = os.path.join(os.path.dirname(max_file_path), '../shp')
        os.makedirs(max_shp_dir_path, exist_ok=True)
        max_shp_file_path = os.path.join(max_shp_dir_path, os.path.basename(max_file_path).replace('.dfs2', '.shp'))
        edit_mzt_file(mzt_file_path, max_file_path, number_of_timesteps, max_shp_file_path, x_max, y_max)
        command = [os.path.join(MIKE_BIN_DIR, "ToolboxShell.exe"), "-run", mzt_file_path, "-x"]
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except Exception as e:
        logging.error(f"Error: max2shp.mzt {e}")

    # shp to geojson
    try:
        os.makedirs(max_shp_dir_path.replace('shp', 'geojson'), exist_ok=True)
        max_shp_file_path_ = f"{os.path.splitext(max_shp_file_path)[0]}_cells_0.shp"
        shp2geojson(max_shp_file_path_, max=True)
    except Exception as e:
        logging.error(f"Error: shp2geojson {e}")


    # max2txt
    try:
        mzt_file_path = os.path.join(API_DIR, 'max2txt.mzt')
        max_txt_dir_path = os.path.join(os.path.dirname(max_file_path), '../txt')
        os.makedirs(max_txt_dir_path, exist_ok=True)
        max_txt_file_path = os.path.join(max_txt_dir_path, os.path.basename(max_file_path).replace('.dfs2', '.txt'))
        edit_mzt_file(mzt_file_path, max_file_path, number_of_timesteps, max_txt_file_path, x_max, y_max)
        command = [os.path.join(MIKE_BIN_DIR, "ToolboxShell.exe"), "-run", mzt_file_path, "-x"]
        p = subprocess.Popen(command, shell=True)
        p.wait()
    except Exception as e:
        logging.error(f"Error: max2txt.mzt {e}")
    