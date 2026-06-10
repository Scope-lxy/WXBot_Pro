"""Email notification support for SiverWXbot_plus."""

import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import time
import os
import sys
from core.logger import log

def _base_dir():
    if hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")

DATA_DIR = os.path.join(_base_dir(), 'data')
email_path = os.path.join(DATA_DIR, 'config', 'email.json')

DEFAULT_EMAIL_CONFIG = {
    "host": "smtp.qq.com",
    "port": 465,
    "user": "1234567890@qq.com",
    "pass": "sqmpasswordsqm",
}


def _normalize_config(config):
    config = config if isinstance(config, dict) else {}
    normalized = {}
    normalized["host"] = str(config.get("host") or DEFAULT_EMAIL_CONFIG["host"]).strip()
    try:
        normalized["port"] = int(config.get("port", DEFAULT_EMAIL_CONFIG["port"]))
    except (TypeError, ValueError):
        normalized["port"] = DEFAULT_EMAIL_CONFIG["port"]
    normalized["user"] = str(config.get("user") or DEFAULT_EMAIL_CONFIG["user"]).strip()
    normalized["pass"] = str(config.get("pass") or DEFAULT_EMAIL_CONFIG["pass"]).strip()
    return normalized


def save_config(config, config_path=email_path):
    """Persist SMTP parameters as JSON and return the normalized config."""
    normalized = _normalize_config(config)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as file:
        json.dump(normalized, file, ensure_ascii=False, indent=4)
    return normalized

def read_config(config_path=email_path):
    """Read SMTP parameters from JSON; create defaults when missing or invalid."""
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            return _normalize_config(json.load(file))
    except Exception as e:
        print(f"配置文件读取失败: {str(e)}\n正在创建默认配置文件：{config_path}")
        try:
            config = save_config(DEFAULT_EMAIL_CONFIG, config_path)
            print(f"默认配置文件email.json已创建：{config_path}")
            return config
        except Exception as write_e:
            print(f"默认配置文件创建失败: {str(write_e)}")
            while True:
                time.sleep(100)

# 配置在每次发送时动态读取，不使用模块级全局变量（避免启动时缓存旧配置）

def send_qq_email(receiver, subject, content):
    '''
    发送QQ邮箱邮件
    receiver: 收件人邮箱
    subject: 邮件主题
    content: 邮件内容
    '''
    # 每次发送时重新读取配置，确保使用最新配置
    cfg = read_config()
    mail_host = cfg['host']
    mail_port = cfg['port']
    mail_user = cfg['user']
    mail_pass = cfg['pass']

    # 创建MIMEText对象构建邮件内容
    message = MIMEText(content, 'plain', 'utf-8')
    message['From'] = Header(mail_user)          # 发件人
    message['To'] = Header(receiver)             # 收件人
    message['Subject'] = Header(subject)         # 主题

    try:
        # 使用SSL加密连接SMTP服务器
        smtpObj = smtplib.SMTP_SSL(mail_host, mail_port)
        # 确保用户名和密码是纯 ASCII 字符串，去除可能的空白字符和 BOM
        user_clean = mail_user.strip().encode('ascii', errors='ignore').decode('ascii')
        pass_clean = mail_pass.strip().encode('ascii', errors='ignore').decode('ascii')
        smtpObj.login(user_clean, pass_clean)      # 登录邮箱
        smtpObj.sendmail(mail_user, receiver, message.as_string())
        print("邮件发送成功")
    except smtplib.SMTPException as e:
        print(f"邮件发送失败，错误：{str(e)}")
    finally:
        smtpObj.quit()

def send_email(receiver=None, subject="默认邮件主题", content="这是来自Python程序的默认邮件内容"):
    '''
    发送邮件 默认给配置中的自己发邮件
    receiver: 收件人邮箱（None 时发给配置文件中的发件人自己）
    subject: 邮件主题
    content: 邮件内容
    '''
    try:
        cfg = read_config()
        if receiver is None:
            receiver = cfg['user']
        send_qq_email(receiver, subject, content)
    except Exception as e:
        log(level="ERROR", message="报错邮件发送失败，请检查邮件配置是否正确")

# 使用示例
if __name__ == "__main__":
    send_qq_email(
        receiver="666666@qq.com",  # 收件邮箱
        subject="测试邮件主题",
        content="这是来自Python程序的测试邮件内容"
    )
