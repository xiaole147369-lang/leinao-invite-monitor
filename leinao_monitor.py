#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
类脑(ΟΔΥΣΣΕΙΑ) Discord 邀请状态监控(本地/云端通用版)
======================================================
监控信号(全部公开接口,无需登录 Discord):
  1. 邀请链接 404(撤销) -> 200(恢复) = 开放/轮换
  2. 宝宝教程页(常用资源页)出现新的 discord.gg 链接且有效
  3. 教程页公告文字出现"开放/恢复邀请"字样
通知: 邮件(检测到开放迹象时)。

邮件配置优先级: 环境变量(SMTP_USER/SMTP_AUTH_CODE/SMTP_TO/SMTP_HOST/SMTP_PORT/SMTP_SSL)
             > 本目录 email_config.json(本地用)
运行参数: 环境变量 GITHUB_ACTIONS=true 时跑一轮即退出(云端),否则常驻循环(本地)。
"""

import argparse
import csv
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

# ---------------- 默认配置(可被同目录 config.json 覆盖) ----------------
CONFIG = {
    "codes": ["ftFV2TCKEx", "odysseia"],       # 监控的邀请码
    "resource_page": "https://down-3ud.pages.dev",  # 类脑宝宝教程/常用资源页
    "join_url": "https://discord.gg/ftFV2TCKEx",    # 通知邮件里带的邀请链接
    "proxy": None,                              # 本地梯子代理,云端保持 None
    "interval_sec": 300,                        # 本地轮询间隔;云端由 GitHub cron 控制节奏
}
# ------------------------------------------------------------------------

API_URL = "https://discord.com/api/v10/invites/{}?with_counts=true"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "leinao_log.csv")
STATE_PATH = os.path.join(HERE, "state.json")
EMAIL_CFG_PATH = os.path.join(HERE, "email_config.json")
CFG_PATH = os.path.join(HERE, "config.json")

OPEN_KEYWORDS = ("开放", "恢复邀请", "重新开放", "再次开放")
CLOSE_KEYWORDS = ("暂停邀请", "停止邀请", "关闭邀请", "满员")

log_lock = threading.Lock()

# 同目录 config.json 覆盖默认值
try:
    with open(CFG_PATH, encoding="utf-8") as f:
        CONFIG.update(json.load(f))
except FileNotFoundError:
    pass


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with log_lock:
        print(line, flush=True)


def fetch(url, timeout=25):
    """有代理走代理,再退直连。HTTP错误码(如404)是有意义的信号,原样返回。"""
    attempts = []
    if CONFIG.get("proxy"):
        attempts.append({"http": CONFIG["proxy"], "https": CONFIG["proxy"]})
    attempts.append({})  # 直连
    for proxies in attempts:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            continue
    return None, None


def check_invite(code):
    """查询邀请码状态。valid=True有效 / False已失效 / None网络原因未知"""
    status, body = fetch(API_URL.format(code))
    if status == 200:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"valid": None, "status": 200, "note": "响应解析失败"}
        prof = data.get("profile") or {}
        guild = data.get("guild") or {}
        channel = data.get("channel") or {}
        return {
            "valid": True,
            "status": 200,
            "guild": guild.get("name") or prof.get("name"),
            "member_count": prof.get("member_count") or guild.get("approximate_member_count"),
            "online_count": prof.get("online_count") or guild.get("approximate_presence_count"),
            "expires_at": data.get("expires_at"),
            "channel": channel.get("name"),
        }
    if status == 404:
        return {"valid": False, "status": 404, "note": "邀请已失效/被撤销"}
    if status is None:
        return {"valid": None, "status": None, "note": "网络不通"}
    return {"valid": None, "status": status, "note": f"HTTP {status}"}


def scan_resource_page():
    """抓取宝宝教程页: 返回(页面上的邀请码列表, 纯文本)"""
    status, body = fetch(CONFIG["resource_page"])
    if status != 200 or not body:
        return None, None
    codes = sorted(set(re.findall(r"discord\.gg/([A-Za-z0-9_-]{4,})", body)))
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    return codes, text


# ---------------- 邮件通知 ----------------

def load_email_config():
    """环境变量优先(云端 GitHub secrets),其次 email_config.json(本地)。"""
    user = os.environ.get("SMTP_USER", "").strip()
    code = os.environ.get("SMTP_AUTH_CODE", "").strip()
    to = os.environ.get("SMTP_TO", "").strip()
    if user and code and to:
        return {
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
            "use_ssl": os.environ.get("SMTP_SSL", "true").lower() != "false",
            "username": user,
            "auth_code": code,
            "to": [x.strip() for x in to.split(",") if x.strip()],
        }
    try:
        with open(EMAIL_CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not cfg.get("username") or not cfg.get("auth_code") or not cfg.get("to"):
        return None
    return cfg


def email_ready():
    return load_email_config() is not None


def send_email(subject, body, cfg=None):
    """发送通知邮件。失败只记录日志,不影响主流程。"""
    cfg = cfg or load_email_config()
    if not cfg:
        log("⚠ 邮件未配置,跳过发送。")
        return False
    host = cfg.get("smtp_host", "smtp.qq.com")
    port = int(cfg.get("smtp_port", 465))
    user, code = cfg["username"], cfg["auth_code"]
    to_list = cfg["to"] if isinstance(cfg["to"], list) else [cfg["to"]]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("类脑邀请监控", user))
    msg["To"] = ", ".join(to_list)

    try:
        if cfg.get("use_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        try:
            server.login(user, code)
            server.sendmail(user, to_list, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        log("📧 通知邮件已发送")  # 不打印收件地址,避免公开日志泄露
        return True
    except Exception as e:
        log(f"⚠ 邮件发送失败: {e!r}")
        return False


def beep():
    try:  # Windows 蜂鸣提醒;Linux/云端静默
        import winsound
        for _ in range(4):
            winsound.Beep(1400, 260)
            winsound.Beep(1800, 260)
    except Exception:
        pass


def alert(reason):
    """检测到开放迹象: 蜂鸣(仅本地) + 发邮件"""
    log(f"🔔 检测到开放迹象: {reason}")
    threading.Thread(target=beep, daemon=True).start()
    url = CONFIG["join_url"]
    subject = f"【类脑邀请监控】邀请可能已开放! {reason[:40]}"
    body = (
        "检测到类脑(ΟΔΥΣΣΕΙΑ)邀请开放迹象,快去看看:\n\n"
        f"触发原因: {reason}\n"
        f"邀请链接: {url}\n"
        f"检测时间: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
        "\n提示: 打开链接后需登录 Discord 并完成新人验证(答题)。\n"
        "如果链接提示无法加入,可能只是轮换预告,请稍后再试或留意教程页。\n"
    )
    send_email(subject, body)


def write_csv(row):
    new = not os.path.exists(CSV_PATH)
    with log_lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["时间", "邀请码", "HTTP状态", "有效性", "成员数",
                            "在线数", "备注"])
            w.writerow(row)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_cycle():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log(f"邮件通知: {'已配置,触发时会正常发信' if email_ready() else '❌ 未配置!请检查仓库 Secrets(SMTP_USER/SMTP_AUTH_CODE/SMTP_TO)'}")

    # --- 信号1: 教程页上的邀请码是否出现新链接 ---
    page_codes, page_text = scan_resource_page()
    state = load_state()
    if page_codes is None:
        log("⚠ 资源页抓取失败(网络问题?),本轮跳过该信号")
    else:
        known = set(state.get("known_page_codes", []))
        fresh = [c for c in page_codes if c not in known]
        if not known:
            log(f"资源页当前邀请链接: {', '.join(page_codes)} (首次记录,基线)")
        elif fresh:
            for c in fresh:
                r = check_invite(c)
                log(f"🆕 资源页出现新邀请链接 {c} -> 有效={r['valid']}")
                if r["valid"]:
                    CONFIG["join_url"] = f"https://discord.gg/{c}"
                    state["join_url_override"] = CONFIG["join_url"]
                    alert(f"教程页发布了新邀请链接 discord.gg/{c},且该链接有效!")
                write_csv([now, c, r.get("status"), r["valid"],
                           r.get("member_count", ""), r.get("online_count", ""),
                           "资源页新链接"])
        state["known_page_codes"] = page_codes

        # --- 信号2: 教程页公告文字里的 开放/暂停 字样 ---
        if page_text:
            opened = [k for k in OPEN_KEYWORDS if k in page_text]
            closed = [k for k in CLOSE_KEYWORDS if k in page_text]
            prev_open = state.get("page_open_seen", False)
            if opened and not prev_open:
                alert(f"教程页出现“{'/'.join(opened)}”字样,邀请可能已开放!")
            state["page_open_words"] = opened
            state["page_open_seen"] = bool(opened)
            if closed and not opened:
                log(f"资源页公告含“{'/'.join(closed)}”字样(关闭状态延续)")

    # --- 信号3: 已知邀请码 404 -> 200 翻转 ---
    last = state.get("code_valid", {})
    for code in CONFIG["codes"]:
        r = check_invite(code)
        valid = r["valid"]
        if valid is None:
            log(f"邀请码 {code}: 状态未知({r.get('note')}),不下结论")
        else:
            prev = last.get(code)
            desc = (f"服务器={r.get('guild')} 成员={r.get('member_count')} "
                    f"在线={r.get('online_count')} 落地频道={r.get('channel')}")
            if valid:
                if prev is False:
                    alert(f"邀请码 {code} 从失效恢复为有效,邀请很可能已开放!({desc})")
                else:
                    log(f"邀请码 {code}: 有效({desc})")
            else:
                log(f"邀请码 {code}: 已失效(404)——类脑撤销链接期间的正常现象,"
                    f"恢复200即为开放信号")
            last[code] = valid
        write_csv([now, code, r.get("status"), valid,
                   r.get("member_count", ""), r.get("online_count", ""),
                   r.get("note", "")])

    state["code_valid"] = last
    state["last_run"] = now
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description="类脑Discord邀请状态监控")
    ap.add_argument("--once", action="store_true", help="只检查一轮就退出")
    ap.add_argument("--interval", type=int, default=None,
                    help="本地轮询间隔秒数(默认300)")
    ap.add_argument("--test-email", action="store_true",
                    help="发送一封测试邮件验证配置后退出")
    args = ap.parse_args()
    if args.interval:
        CONFIG["interval_sec"] = args.interval

    if args.test_email:
        if not email_ready():
            log("❌ 邮件未配置(环境变量或 email_config.json),无法发送。")
            sys.exit(1)
        ok = send_email("【类脑邀请监控】测试邮件",
                        "这是一封测试邮件。收到即说明邮件通知配置成功,\n"
                        "检测到类脑邀请开放时会收到类似邮件。\n")
        sys.exit(0 if ok else 1)

    on_cloud = os.environ.get("GITHUB_ACTIONS") == "true"
    if on_cloud:
        log("云端单次检查(GitHub Actions)")
        check_cycle()
        return

    log("=" * 62)
    log("类脑(ΟΔΥΣΣΕΙΑ)邀请监控已启动")
    log(f"  监控邀请码 : {', '.join(CONFIG['codes'])}")
    log(f"  资源页     : {CONFIG['resource_page']}")
    log(f"  代理       : {CONFIG.get('proxy') or '直连'}")
    log(f"  轮询间隔   : {CONFIG['interval_sec']} 秒  (Ctrl+C 停止)")
    log(f"  邮件通知   : {'已配置' if email_ready() else '❌ 未配置(环境变量或 email_config.json)'}")
    log(f"  日志文件   : {CSV_PATH}")
    log("=" * 62)
    if not email_ready():
        log("提醒: 不配邮件也能运行(看窗口/日志),但收不到邮件提醒。")

    if args.once:
        check_cycle()
        return

    while True:
        try:
            check_cycle()
        except Exception as e:
            log(f"⚠ 本轮检查出错: {e!r}")
        try:
            time.sleep(CONFIG["interval_sec"])
        except KeyboardInterrupt:
            log("已手动停止监控。")
            break


if __name__ == "__main__":
    main()
