import requests
import datetime

if __name__ == "__main__":
    dt = datetime.datetime.now()
    year = dt.year
    month = dt.month
    day = dt.day
    for hour in [0, 12]:
        start_hour = 0
        run_hours = 120
        url = f"http://127.0.0.1:8000/download?year={year}&month={month}&day={day}&hour={hour}&start_hour={start_hour}&run_hours={run_hours}"
        res = requests.get(url)
        if res.status_code = 200:
            print(f"{dt}: OK")
        else:
            print(f"{dt}: ERROR")