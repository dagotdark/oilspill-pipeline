import os
import datetime
import pandas as pd
import numpy as np
from mikeio import Dataset
from mikeio.eum import EUMType, EUMUnit, ItemInfo
from pydantic import BaseModel


class Source(BaseModel):
    result_folder: str = ''
    source_datetime: str = '2024-10-10 13:00:00'
    start_hour: int = 0
    run_hours: int = 120
    time_step_interval: int = 180
    xyz: list = [[325201.53349209955, 4296170.859318475, 1.0],
                 [325201.53349209955, 4295170.859318475, 75.0],
                 [325201.53349209955, 4294170.859318475, 13.0],
                 [326201.53349209955, 4294170.859318475, 16.0]]


# Create source.dfs0
def create_source(dfs_dir, start_datetime, run_hours, time_step_interval, source):
    # Read source template file
    with open('source_template.txt', 'r') as file:
        data_tmp = file.read()
        num_sources = min(len(source.xyz), 62) # Sources limit of MIKE
        new_data = f'Touched = 1\n         MzSEPfsListItemCount = {num_sources}\n         number_of_sources = {num_sources}\n'
        for i, (x, y, z) in enumerate(source.xyz):
            data = data_tmp.replace('<source_num>', str(i+1))
            data = data.replace('x_coord', str(x))
            data = data.replace('y_coord', str(y))
            source_file_path = create_dfs0(dfs_dir, start_datetime, run_hours, time_step_interval, source.source_datetime, i+1, z)
            data = data.replace('<source_file_path_tmp>', source_file_path)
            new_data += data + '\n\n'
            # Sources limit of MIKE
            if i+1 >= 62:
                break
    
    return new_data


def create_dfs0(dfs_dir, start_datetime, run_hours, time_step_interval, source_datetime, source_num, oil_mass):
   
    end_datetime = start_datetime + datetime.timedelta(hours=run_hours)

    times = pd.date_range(start_datetime, end_datetime, freq=datetime.timedelta(seconds=time_step_interval))

    data = np.full((8, len(times)), np.nan)
    
    volatile_mass = oil_mass * 0.3
    heavy_mass = oil_mass * 0.6899
    wax_content = oil_mass * 0.01
    asphaltene = oil_mass * 0.0001
    water_fraction_of_oil = 0
    buoyancy = 0.001
    surface_area_of_oil = 1000
    particle_in_water_phase = 1

    idx = times.get_indexer([pd.to_datetime(source_datetime)], method='nearest')[0]
    data[:, idx] = [
        volatile_mass,
        heavy_mass,
        wax_content,
        asphaltene,
        water_fraction_of_oil,
        buoyancy,
        surface_area_of_oil,
        particle_in_water_phase
    ]
    
    items = [
        ItemInfo(name='Volatile mass', itemtype=EUMType.Mass_per_Time, unit=EUMUnit.kg_per_sec, data_value_type='Instantaneous'), 
        ItemInfo(name='Heavy mass', itemtype=EUMType.Mass_per_Time, unit=EUMUnit.kg_per_sec, data_value_type='Instantaneous'), 
        ItemInfo(name='Wax content', itemtype=EUMType.Mass_per_Time, unit=EUMUnit.kg_per_sec, data_value_type='Instantaneous'), 
        ItemInfo(name='Asphaltene', itemtype=EUMType.Mass_per_Time, unit=EUMUnit.kg_per_sec, data_value_type='Instantaneous'), 
        ItemInfo(name='Water fraction of oil', itemtype=EUMType.Concentration_Non_Dim, data_value_type='Instantaneous'), 
        ItemInfo(name='Buoyancy: average diameter of oil droplets', itemtype=EUMType.Grain_diameter, unit=EUMUnit.meter, data_value_type='Instantaneous'), 
        ItemInfo(name='Surface area of oil', itemtype=EUMType.Element_area, unit=EUMUnit.meter_pow_2, data_value_type='Instantaneous'), 
        ItemInfo(name='Particle in water phase', itemtype=EUMType.Undefined, unit=EUMUnit.undefined, data_value_type='Instantaneous'), 
    ]
    source_dfs0 = Dataset(data, time=times, items=items)
    # Write the dataset to a DFS0 file
    source_dfs0_file = f"{start_datetime.strftime('%Y%m%d%H')}_{end_datetime.strftime('%Y%m%d%H')}_source_{source_num:02}.dfs0"
    source_dfs0_file_path = os.path.join(dfs_dir, source_dfs0_file)
    if not os.path.isfile(source_dfs0_file_path):
        source_dfs0.to_dfs(source_dfs0_file_path)
        print(f"Source file created {source_dfs0_file}")

    return source_dfs0_file_path