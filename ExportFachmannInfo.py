import os
import time
import json
from threading import Lock
from timeit import default_timer as timer

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

debug = False

MAP_METRICS = {
    "Zeitstempel": {"name": "timestamp", "type": "info"},
    "Außentemperatur Aktuell": {"name": "aussentemperatur_aktuell", "type": "gauge", "strip": len(" °C")},
    "Warmwassertemperatur Aktuell": {"name": "warmwassertemperatur_aktuell", "type": "gauge", "strip": len(" °C")},
}

chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--disable-gpu")


def refresh_page(driver):
    if (debug):
        print("Refreshing page...")
    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.ID, "ctl00_DeviceContextControl1_RefreshDeviceDataButton"))
    ).click()
    wait_until_page_loaded(driver)


def login_and_load_fachmann_page(driver):
    wemportal_user = "YOUR_USERNAME"
    wemportal_password = "YOUR_PASSWORD"
    fachmann_password = "11"

    driver.get("https://www.wemportal.com/Web/")
    if (debug):
        print("Logging in...")
    driver.find_element(By.ID, "ctl00_content_tbxUserName").click()
    driver.find_element(By.ID, "ctl00_content_tbxUserName").send_keys(wemportal_user)
    driver.find_element(By.ID, "ctl00_content_tbxPassword").send_keys(wemportal_password)
    driver.find_element(By.ID, "ctl00_content_btnLogin").click()
    if (debug):
        print("Go to Fachmann info page...")
    driver.find_element(By.CSS_SELECTOR, "#ctl00_RMTopMenu > ul > li.rmItem.rmFirst > a > span").click()
    driver.find_element(By.CSS_SELECTOR, "#ctl00_SubMenuControl1_subMenu > ul > li:nth-child(4) > a > span").click()
    driver.switch_to.frame(0)
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "ctl00_DialogContent_tbxSecurityCode"))
    ).click()
    driver.find_element(By.ID, "ctl00_DialogContent_tbxSecurityCode").send_keys(fachmann_password)
    driver.find_element(By.ID, "ctl00_DialogContent_BtnSave").click()
    driver.switch_to.default_content()


def wait_until_page_loaded(driver):
    timeout = time.time() + 60*2   # 2 minutes from now
    while True:
        refresh_button_span = driver.find_element(By.ID, "ctl00_DeviceContextControl1_RefreshDeviceDataButton")
        if (debug):
            print("Waiting for page loaded...".format(refresh_button_span.id), end="")
        start = timer()
        try:
            WebDriverWait(driver, 8, poll_frequency=0.2).until(
                EC.staleness_of(refresh_button_span)
            )
            if (debug):
                print("took {}".format(timer() - start))
        except TimeoutException:
            if (debug):
                print("timed out")
            break
        if (time.time() > timeout):
            if (debug):
                print("Data got not loaded within 2 minutes, stopping it!")
            break
    if (debug):
        print("Page loaded")


def parse_page(driver):
    timestamp = driver.find_element(By.ID, "ctl00_DeviceContextControl1_lblDeviceLastDataUpdateInfo").text
    result = {"Zeitstempel": timestamp}
    if (debug):
        print("Parsing page with timestamp {}".format(timestamp))

    map_id_to_name = {}

    for element in driver.find_elements(By.CLASS_NAME, "simpleDataName"):
        stripped_id = element.get_attribute("id")[:-8]
        value = element.text
        map_id_to_name[stripped_id] = value

    for element in driver.find_elements(By.CLASS_NAME, "simpleDataValue"):
        stripped_id = element.get_attribute("id")[:-9]
        value = element.text.replace(",", ".")
        result[map_id_to_name[stripped_id]] = value
    if (debug):
        print("Found {} data points".format(len(result)))

    return result


def parse_value(value, strip=None):
    if value == "Aus" or value == "--":
        return 0
    elif strip is not None:
        return float(value[:-int(strip)])
    else:
        return float(value)

def collect_metrics_from_page(driver):
    result = parse_page(driver)

    data = {}

    for key, value in result.items():
        metric = MAP_METRICS.get(key)
        if metric is not None:
            name = metric["name"]
            t = metric.get("type", "gauge")
            if t is "gauge":
                value = parse_value(value, metric.get("strip"))
                data[name] = value
            if t is "counter":
                value = parse_value(value, metric.get("strip"))
                data[name] = value
            if t is "info":
                data[name] = value

    return json.dumps(data)

class CustomCollector(object):
    def __init__(self):
        self.lock = Lock()
        self.driver = None
        self.refreshed = False
        self.collections_done = 0
        self.start_driver()

    def collect(self):
        self.lock.acquire()
        try:
            return self.collect_metrics()
        finally:
            self.lock.release()

    def collect_metrics(self, retries_left=3):
        try:
            self.ensure_driver_restarted()
            self.ensure_refreshed()
            metrics = collect_metrics_from_page(self.driver)
            if (debug):
                print("Exporting {} metrics".format(len(metrics)))
            self.collections_done = self.collections_done + 1
            return metrics
        except WebDriverException as e:
            if (debug):
                print("Encountered web driver exception:")
                print(e)
            if retries_left == 0:
                if (debug):
                    print("No retries left, bailing out")
                raise e
            if (debug):
                print("Restarting driver... (retries_left={})".format(retries_left))
            self.restart_driver()
            return self.collect_metrics(retries_left - 1)
        finally:
            self.refreshed = False

    def ensure_driver_restarted(self):
        if self.collections_done <= 200:
            return
        try:
            if (debug):
                print("Restarting driver as {} collections done".format(self.collections_done))
            self.restart_driver()
        finally:
            self.collections_done = 0

    def ensure_refreshed(self):
        if not self.refreshed:
            refresh_page(self.driver)
            self.refreshed = True

    def restart_driver(self):
        self.driver.quit()
        self.start_driver()

    def start_driver(self):
        self.driver = webdriver.Chrome(options=chrome_options)
        login_and_load_fachmann_page(self.driver)
        wait_until_page_loaded(self.driver)
        self.refreshed = True

    def __del__(self):
        if (debug):
            print("Shutting down...")
        try:
            self.driver.quit()
        except Exception:
            pass
        finally:
            os.system('pkill chromedriver')


if __name__ == "__main__":
    customcollector = CustomCollector()
    metrics = customcollector.collect_metrics()
    print(metrics)
