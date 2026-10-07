import requests
import yaml
import random
from datetime import datetime, timedelta
import json
import os
import logging

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from urllib.parse import urlsplit
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import time


logging.basicConfig(
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)

WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
BOOK_DAYS_AHEAD = 2  # 预约后天
ALREADY_BOOKED_KEYS = ("已有预约", "重复预约", "请先取消", "当前已有", "只能预约一个", "已存在预约")
SEAT_TAKEN_KEYS = ("已被", "占用", "不可用", "不存在", "冲突")


def target_weekday_name():
    return WEEKDAYS[(datetime.now().weekday() + BOOK_DAYS_AHEAD) % 7]


def is_booking_enable(date_cfg):
    if date_cfg['启用']:
        return True
    return False


def expand_seat_cfg(cfg):
    ids = []
    for begin, end in cfg['ranges']:
        ids.extend(range(begin, end + 1))
    return ids


def get_seats_with_config(user_config, date_config, seat_config):
    seat_name = date_config['name']
    if seat_name == "自定义":
        return user_config['自定义']
    return expand_seat_cfg(seat_config[seat_name])


def slot_label(start, hours):
    return "{:g}:00-{:g}:00".format(start, start + hours)


# 一天要么配 开始时间/持续小时数 一个时段，要么配「时间段」列表约多段
def get_time_slots(date_config):
    if '时间段' in date_config:
        if '开始时间' in date_config or '持续小时数' in date_config:
            raise ValueError("「时间段」与「开始时间/持续小时数」不能同时配置，请只保留一种")
        if not date_config['时间段']:
            raise ValueError("「时间段」为空，请至少填一个时段")
        slots = [(item['开始时间'], item['持续小时数']) for item in date_config['时间段']]
    else:
        slots = [(date_config['开始时间'], date_config['持续小时数'])]
    for _, hours in slots:
        if hours <= 0:
            raise ValueError("持续小时数必须大于 0")
    slots = sorted(slots)
    for (prev_start, prev_hours), (next_start, _) in zip(slots, slots[1:]):
        if next_start < prev_start + prev_hours:
            raise ValueError("时间段重合：{} 与 {}:00 起".format(
                slot_label(prev_start, prev_hours), next_start))
    return slots


class SeatAutoBooker:
    def __init__(self, booker_config):
        self.json = None
        self.resp = None
        self.user_data = None

        logging.info('Creating SeatAutoBooker object')

        self.un = os.environ["SCHOOL_ID"].strip()  # 学号
        print("使用用户：{}".format(self.un))
        self.pd = os.environ["PASSWORD"].strip()  # 密码
        self.SCKey = None
        try:
            self.SCKey = os.environ["SCKEY"]
        except KeyError:
            print("没有Server酱的key,将不会推送消息")

        chrome_options = Options()
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "")
        if not chromedriver_path:
            for candidate in ('/usr/local/bin/chromedriver', '/opt/homebrew/bin/chromedriver'):
                if os.path.exists(candidate):
                    chromedriver_path = candidate
                    break
        service = Service(chromedriver_path) if chromedriver_path else Service()
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.wait = WebDriverWait(self.driver, 10, 0.5)
        self.cookie = None

        self.cfg = booker_config

    def book_favorite_seat(self, user_config, seat_config):
        results = []
        preferred_seat = None
        for start, hours in get_time_slots(user_config[target_weekday_name()]):
            label = slot_label(start, hours)
            print("开始预约时段 {}".format(label))
            result = self._book_slot(user_config, seat_config, start, hours, preferred_seat)
            if result:
                code, message, seat = result
                preferred_seat = seat
                results.append((label, code, message))
            else:
                results.append((label, -1, "预约失败"))
        return results

    def _book_slot(self, user_config, seat_config, start, hours, preferred_seat=None):
        retry_sleep_time = timedelta(minutes=self.cfg["cron-delta-minutes"]).seconds*2/(self.cfg["max-retry"]-2) - 10
        for tried_times in range(self.cfg["max-retry"]):
            try:
                result = self._book_favorite_seat(user_config, seat_config, start, hours, tried_times, preferred_seat)
                msg = str(result[1]) if result else ""
                if result and any(k in msg for k in ALREADY_BOOKED_KEYS):
                    print("该时段已有预约，跳过：{}".format(result[1]))
                    return result
                if result and any(k in msg for k in ("频繁", "人数过多")):
                    print("触发限流({})，{:.0f}秒后重试".format(result[1], retry_sleep_time))
                    time.sleep(retry_sleep_time)
                    continue
                if result and any(k in msg for k in SEAT_TAKEN_KEYS):
                    print("座位不可约({})，换一个再试".format(result[1]))
                    continue
                return result
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "尝试第{}次".format(tried_times))
                time.sleep(retry_sleep_time)

    def _book_favorite_seat(self, user_config, seat_config, start, hours, tried_times=0, preferred_seat=None):
        logging.info('Entering _book_favorite_seat method')
        date_config = user_config[target_weekday_name()]
        seats = get_seats_with_config(user_config, date_config, seat_config)
        today_0_clock = datetime.strptime(datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S")
        book_time = today_0_clock + timedelta(days=BOOK_DAYS_AHEAD) + timedelta(hours=start)
        delta = book_time - self.cfg["start-time"]
        total_seconds = delta.days * 24 * 3600 + delta.seconds
        # 第一轮优先沿用上一时段约到的座位，被占后再随机换
        if tried_times == 0 and preferred_seat in seats:
            seat = preferred_seat
        elif date_config['name'] == '自定义' and tried_times<self.cfg["max-retry"]/3*2:
            seat = seats[0]
        else:
            seat = random.choice(seats)
        data = f"beginTime={total_seconds}&duration={3600 * hours}&&seats[0]={seat}&seatBookers[0]={self.user_data['uid']}"

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        print(data)
        self.resp = requests.post(self.cfg["target"], data=data, headers=headers)
        self.json = json.loads(self.resp.text)
        return self.json["CODE"], self.json["MESSAGE"] + " 座位:{}".format(seat), seat

    def login(self):
        logging.info('Login in')

        try:
            logging.info('开始登陆...')

            self.driver.get("https://hdu.huitu.zhishulib.com/")
            self.wait.until(lambda d: "sso.hdu.edu.cn" in d.current_url)

            # 排除同名隐藏域
            form_wait = WebDriverWait(self.driver, 30, 0.5)
            user_el = form_wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[name='username']:not([type='hidden'])")
            ))

            # 关掉公告弹窗
            for close_button in self.driver.find_elements(
                By.CSS_SELECTOR, "img.icon-close, .ant-modal-close"
            ):
                if close_button.is_displayed() and close_button.is_enabled():
                    close_button.click()
                    self.wait.until(EC.invisibility_of_element(close_button))

            user_el.clear()
            user_el.send_keys(self.un)
            logging.info('输入用户名')

            pwd_el = form_wait.until(EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "input[type='password']")))
            pwd_el.clear()
            pwd_el.send_keys(self.pd)
            logging.info('输入密码')

            # 失焦后再点登录
            pwd_el.send_keys(Keys.TAB)
            form_wait.until(lambda d: "disabled" not in d.find_element(
                By.CSS_SELECTOR, "button.login-button").get_attribute("class"))
            self.driver.find_element(By.CSS_SELECTOR, "button.login-button").click()
            logging.info('点击登录按钮')

            # 等跳回图书馆域名
            WebDriverWait(self.driver, 30, 0.5).until(
                lambda d: urlsplit(d.current_url).hostname == "hdu.huitu.zhishulib.com")
            time.sleep(5)
            cookie_list = self.driver.get_cookies()
            self.cookie = ";".join([item["name"] + "=" + item["value"] + "" for item in cookie_list])
            self.cfg["headers"]['Cookie'] = self.cookie

            logging.info("登录成功！")
        except Exception as e:
            logging.error(f"登录失败：{e}")
            return -1
        return 0

    def get_user_info(self):
        logging.info('Getting user info')

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        try:
            resp = requests.get("https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1",
                                headers=headers)
            self.user_data = resp.json()['DATA']
            _ = self.user_data['uid']
        except Exception as e:
            logging.exception(e)
            print(self.user_data)
            print(e.__class__.__name__ + ",获取用户数据失败")
            return -1
        print("获取用户数据成功")
        return 0

    def wechatNotice(self, message, desp=None):
        logging.info('Sending WeChat notice')

        if self.SCKey != '':
            url = 'https://sctapi.ftqq.com/{0}.send'.format(self.SCKey)
            data = {
                'title': message,
                desp: desp,
            }
            try:
                r = requests.post(url, data=data)
                if r.json()["data"]["error"] == 'SUCCESS':
                    print("Server酱通知成功")
                else:
                    print("Server酱通知失败")
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "推送服务配置错误")

if __name__ == "__main__":
    logging.info('Start of the program')
    with open("user_config.yml", 'r') as f_obj:
        user_config = yaml.safe_load(f_obj)
    with open("config/basic_config.yml", 'r') as f_obj:
        basic_config = yaml.safe_load(f_obj)
    with open("config/seat_config.yml", 'r') as f_obj:
        seat_config = yaml.safe_load(f_obj)

    if not is_booking_enable(user_config[target_weekday_name()]):
        logging.info('预约未启用')
        print("预约未启用")
        exit(0)

    try:
        get_time_slots(user_config[target_weekday_name()])
    except (ValueError, KeyError, TypeError) as e:
        print("user_config.yml 中「{}」的时段配置有误：{}".format(target_weekday_name(), e))
        exit(-1)

    s = SeatAutoBooker(basic_config["SeatAutoBooker"])
    if not s.login() == 0:
        s.driver.quit()
        logging.info('Login unsuccessful')
        exit(-1)
    if not s.get_user_info() == 0:
        s.driver.quit()
        logging.info('Getting user info unsuccessful')
        exit(-1)
    results = s.book_favorite_seat(user_config=user_config, seat_config=seat_config)
    for slot, code, message in results:
        print("预约结果[{}]: {} {}".format(slot, code, message))
    s.driver.quit()
    logging.info('End of the program')
