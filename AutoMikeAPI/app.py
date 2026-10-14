import os
import uvicorn
import time
import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from utils import download_salinity, download, download_history, grib_to_dfs, calc_hd, calc_os, check_oper_icbc
from source import Source
import logging

logging.basicConfig(level=logging.INFO, filename="app.log", filemode="a",
                    format="%(asctime)s %(levelname)s %(message)s")


# Initialize API
app = FastAPI(title="MIKE model run service")
# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

message = {"status": "Init"}

@app.get("/check_status", response_description="Check status")
async def check_status_endpoint():
    return message


# Download GFS data
def run_download_salinity(year, month, day, hour, run_hours):
    try:
        s_t = time.time()
        message["status"] = "Downloading salinity"
        logging.info("Downloading salinity")
        download_salinity(year, month, day, hour, run_hours)
        message["status"] = "Download salinity complete"
        logging.info(f"Download salinity complete, elapsed time: {time.time() - s_t}")
    except Exception as e:
        message["status"] = "Error downloading salinity"
        message["detail"] = str(e)
        logging.error(f"Downloading salinity: {e}")


@app.get("/download_salinity", response_description="Run download salinity")
async def download_salinity_endpoint(year: int, month: int, day: int, hour: int, run_hours: int, background_tasks: BackgroundTasks):
    try:
        message["detail"] = ""
        if hour < 0 or hour > 23:
            raise Exception("Invalid hour, must be between 0 and 23")

        background_tasks.add_task(run_download_salinity, year, month, day, hour, run_hours)
        message["status"] = "Start downloading salinity"
        return message
    
    except Exception as e:
        message["status"] = "Error downloading salinity"
        message["detail"] = str(e)
        logging.error(f"Downloading salinity: {e}")
        raise HTTPException(500, str(e))
    

# Download GFS operational data
def run_download(year, month, day, hour, start_hour, run_hours, history=False):
    try:
        s_t = time.time()
        message["status"] = "Downloading"
        logging.info("Downloading")
        if history:
            download_history(year, month, day, hour, start_hour, run_hours)
        else:
            download(year, month, day, hour, start_hour, run_hours)
        message["status"] = "Download complete"
        logging.info(f"Download complete, elapsed time: {time.time() - s_t}")
    except Exception as e:
        message["status"] = "Error downloading"
        message["detail"] = str(e)
        logging.error(f"Downloading: {e}")


@app.get("/download", response_description="Run download")
async def download_endpoint(year: int, month: int, day: int, hour: int, start_hour: int, run_hours: int, background_tasks: BackgroundTasks):
    try:
        message["detail"] = ""
        history = False
        today = datetime.datetime.now()
        user_date = datetime.datetime(year, month, day, hour)
        if hour not in [0, 6, 12, 18]:
            raise Exception("Invalid hour, must be 0, 6, 12 or 18")
        if user_date > today - datetime.timedelta(hours=6):
            raise Exception(f"Invalid date, must be greater than {(today - datetime.timedelta(hours=6)).date()}")
        # Historical
        if user_date < today - datetime.timedelta(days=10) and\
           (not check_oper_icbc(user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours)):
            if start_hour != 0:
                raise Exception("Invalid start_hour, must be 0")
            if run_hours < 2 or (run_hours % 3 != 0):
                raise Exception("Invalid run_hours, must be greater than 2 and multiple of 3")
            history = True
        # Operational
        else: 
            if start_hour < 0 or start_hour > 120:
                raise Exception("Invalid start_hour, must be between 0 and 120")
            if run_hours < 1 or run_hours > 120:
                raise Exception("Invalid run_hours, must be between 1 and 120")
        
        background_tasks.add_task(run_download, user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours, history)
        message["status"] = "Start downloading"
        return message
    
    except Exception as e:
        message["status"] = "Error downloading"
        message["detail"] = str(e)
        logging.error(f"Downloading: {e}")
        raise HTTPException(500, str(e))
    
    
# Convert grib to dfs
def run_grib_to_dfs(year, month, day, hour, start_hour, run_hours, source=None, history=False):
    # Download meteo data
    run_download(year, month, day, hour, start_hour, run_hours, history)

    if message["status"] == "Download complete":
        try:
            s_t = time.time()
            message["status"] = "Converting grib to dfs"
            logging.info("Converting grib to dfs")
            grib_to_dfs(year, month, day, hour, start_hour, run_hours, source, history)
            message["status"] = "Conversion of grib to dfs complete"
            logging.info(f"Conversion of grib to dfs complete, elapsed time: {time.time() - s_t}")
        except Exception as e:
            message["status"] = "Error conversion of grib to dfs"
            message["detail"] = str(e)
            logging.error(f"Converting grib to dfs: {e}")


@app.get("/grib_to_dfs", response_description="Convert grib to dfs")
async def grib_to_dfs_endpoint(year: int, month: int, day: int, hour: int, start_hour: int, run_hours: int, background_tasks: BackgroundTasks):
    try:
        message["detail"] = ""
        history = False
        today = datetime.datetime.now()
        user_date = datetime.datetime(year, month, day, hour)
        if hour not in [0, 6, 12, 18]:
            raise Exception("Invalid start_hour, must be 0, 6, 12 or 18")
        if user_date > today - datetime.timedelta(hours=6):
            raise Exception(f"Invalid date, must be less than {(today - datetime.timedelta(hours=6)).date()}")
        # Historical
        if user_date < today - datetime.timedelta(days=10) and\
           (not check_oper_icbc(user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours)):
            if start_hour != 0:
                raise Exception("Invalid start_hour, must be 0")
            if run_hours < 2 or (run_hours % 3 != 0):
                raise Exception("Invalid run_hours, must be greater than 2 and multiple of 3")
            history = True
        # Operational
        else:
            if start_hour < 0 or start_hour > 120:
                raise Exception("Invalid start_hour, must be between 0 and 120")
            if run_hours < 1 or run_hours > 120:
                raise Exception("Invalid run_hours, must be between 1 and 120")
        background_tasks.add_task(run_grib_to_dfs, user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours, history=history)
        message["status"] = "Start converting grib to dfs"
        return message
    
    except Exception as e:
        message["status"] = "Error conversion of grib to dfs"
        message["detail"] = str(e)
        logging.error(f"Converting grib to dfs: {e}")
        raise HTTPException(500, str(e))
    

# Hydrodynamics calculation
def run_hd(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, history=False):

    # Convert grib to dfs2
    run_grib_to_dfs(year, month, day, hour, start_hour, run_hours, source, history)

    if message["status"] == "Conversion of grib to dfs complete":
        try:
            s_t = time.time()
            message["status"] = "Hydrodynamics calculation"
            logging.info("Hydrodynamics calculation")
            if calc_hd(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source):
                message["status"] = "Hydrodynamics calculation complete"
                logging.info(f"Hydrodynamics calculation complete, elapsed time: {time.time() - s_t}")
            else:
                message["status"] = "Error hydrodynamics calculation"
                message["detail"] = "See hydro.log"
                logging.error(f"Hydrodynamics calculation: See hydro.log")
        except Exception as e:
            message["status"] = "Error hydrodynamics calculation"
            message["detail"] = str(e)
            logging.error(f"Hydrodynamics calculation: {e}")


@app.get("/calc_hydrodynamics", response_description="Hydrodynamics calculation")
async def hd_endpoint(year: int, month: int, day: int, hour: int, start_hour: int, run_hours: int, time_step_interval: int, background_tasks: BackgroundTasks):
    try:
        message["detail"] = ""
        history = False
        today = datetime.datetime.now()
        user_date = datetime.datetime(year, month, day, hour)
        if hour not in [0, 6, 12, 18]:
            raise Exception("Invalid start_hour, must be 0, 6, 12 or 18")
        if user_date > today - datetime.timedelta(hours=6):
            raise Exception(f"Invalid date, must be greater than {(today - datetime.timedelta(hours=6)).date()}")
        # Historical
        if user_date < today - datetime.timedelta(days=10) and\
           (not check_oper_icbc(user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours)):
            if start_hour != 0:
                raise Exception("Invalid start_hour, must be 0")
            if run_hours < 2 or (run_hours % 3 != 0):
                raise Exception("Invalid run_hours, must be greater than 2 and multiple of 3")
            history = True
        # Operational
        else:
            if start_hour < 0 or start_hour > 120:
                raise Exception("Invalid start_hour, must be between 0 and 120")
            if run_hours < 1 or run_hours > 120:
                raise Exception("Invalid run_hours, must be between 1 and 120")
        if time_step_interval < 1:
            raise Exception("time_step_interval must be greater than 0")
        if (run_hours * 3600) < time_step_interval:
            raise Exception(f"time_step_interval must be less than run_hours in seconds {run_hours * 3600}")
        number_of_timesteps = int(run_hours * 3600 / time_step_interval)
    
        background_tasks.add_task(run_hd, user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, history)
        message["status"] = "Start hydrodynamics calculation"
        return message
    
    except Exception as e:
        message["status"] = "Error hydrodynamics calculation"
        message["detail"] = str(e)
        logging.error(f"Hydrodynamics calculation: {e}")
        raise HTTPException(500, str(e))
      

# Oil Spill calculation
def run_os(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, history=False):

    # Run hydrodynamics
    run_hd(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, history)

    if message["status"] == "Hydrodynamics calculation complete":
        try:
            s_t = time.time()
            message["status"] = "Oil Spill calculation"
            logging.info("Oil Spill calculation")
            if calc_os(year, month, day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source):
                message["status"] = "Oil Spill calculation complete"
                logging.info(f"Oil Spill calculation complete, elapsed time: {time.time() - s_t}")
            else:
                message["status"] = "Error oil spill calculation"
                message["detail"] = "See oil.log"
                logging.error(f"Oil Spill calculation: See oil.log")
        except Exception as e:
            message["status"] = "Error oil spill calculation"
            message["detail"] = str(e)
            logging.error(f"Oil Spill calculation: {e}")


@app.post("/calc_oil_spill", response_description="Oil Spill calculation")
async def os_endpoint(source: Source, background_tasks: BackgroundTasks):
    try:
        message["detail"] = ""
        history = False
        user_date = datetime.datetime.strptime(source.source_datetime, '%Y-%m-%d %H:%M:%S') - datetime.timedelta(hours=12)
        user_date = user_date - datetime.timedelta(hours=user_date.hour%6)
        hour = user_date.hour
        start_hour = source.start_hour
        run_hours = source.run_hours
        time_step_interval = source.time_step_interval
        today = datetime.datetime.now()
        if hour not in [0, 6, 12, 18]:
            raise Exception("Invalid start_hour, must be 0, 6, 12 or 18")
        if user_date > today - datetime.timedelta(hours=6):
            raise Exception(f"Invalid date, must be greater than {(today - datetime.timedelta(hours=6)).date()}")
        # Historical
        if user_date < today - datetime.timedelta(days=10) and\
           (not check_oper_icbc(user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours)):
            if start_hour != 0:
                raise Exception("Invalid start_hour, must be 0")
            if run_hours < 2 or (run_hours % 3 != 0):
                raise Exception("Invalid run_hours, must be greater than 2 and multiple of 3")
            history = True
        # Operational
        else:
            if start_hour < 0 or start_hour > 120:
                raise Exception("Invalid start_hour, must be between 0 and 120")
            if run_hours < 1 or run_hours > 120:
                raise Exception("Invalid run_hours, must be between 1 and 120")
        if time_step_interval < 1:
            raise Exception("time_step_interval must be greater than 0")
        if (run_hours * 3600) < time_step_interval:
            raise Exception(f"time_step_interval must be less than run_hours in seconds {run_hours * 3600}")
        number_of_timesteps = int(run_hours * 3600 / time_step_interval)
        if len(source.xyz) == 0:
            raise Exception("Source xyz is empty")

        background_tasks.add_task(run_os, user_date.year, user_date.month, user_date.day, hour, start_hour, run_hours, time_step_interval, number_of_timesteps, source, history)
        message["status"] = "Start oil spill calculation"
        return message
    
    except Exception as e:
        message["status"] = "Error oil spill calculation"
        message["detail"] = str(e)
        logging.error(f"Oil Spill calculation: {e}")
        raise HTTPException(500, str(e))
    
    

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)