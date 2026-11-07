#!/usr/bin/env python3
"""保存并检查佳明个人 API 密钥。

用法（在你自己的终端里运行，不要把密钥发到聊天里）：
  python3 garmin_setup.py setup    # 粘贴密钥（输入时不显示），自动判断国际区/中国区并测试
  python3 garmin_setup.py check    # 检查密钥是否可用（智能体可以运行，不会显示密钥）
  python3 garmin_setup.py forget   # 删除本机保存的密钥和令牌缓存

密钥保存在 ~/.config/garmin/connect-personal-api-key（权限 600），
与佳明官方 Python 客户端的默认位置相同。
"""
import getpass
import sys
import time
from datetime import datetime

sys.path.insert(0, __import__("os").path.dirname(__file__))
import gclib  # noqa: E402


def _expiry_text(exp):
    if not exp:
        return "未知"
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")


def cmd_setup():
    if not sys.stdin.isatty():
        print("这个命令需要你本人在终端里运行，这样密钥不会出现在聊天记录里。\n"
              "请打开“终端”（Mac：启动台搜索“终端”；Windows：PowerShell），粘贴运行：\n"
              "  python3 \"%s\" setup" % __file__)
        return 2
    print("请到佳明网页版创建个人 API 密钥，复制后粘贴到这里：")
    print("  国际区账号：" + gclib.KEY_PAGES["global"])
    print("  中国区账号：" + gclib.KEY_PAGES["cn"])
    key = getpass.getpass("粘贴密钥后按回车（输入时不显示）：").strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        print("看起来不完整（太短或带空格）。请重新复制后再试。")
        return 1
    print("正在测试……")
    try:
        region, tok, exp = gclib.detect_region(key)
    except gclib.GarminError as e:
        print("测试失败：%s" % e)
        print("密钥没有保存。常见原因：复制不完整；密钥已删除或过期；网络不通。")
        return 1
    gclib.save_key(key)
    cfg = gclib.load_config()
    cfg["region"] = region
    gclib.save_config(cfg)
    gclib.save_json(gclib.TOKEN_CACHE, {"fp": gclib.key_fingerprint(key), "host": gclib.HOSTS[region],
                                        "token": tok, "exp": exp or time.time() + 23 * 3600, "region": region})
    print("成功。地区：%s。" % ("中国区" if region == "cn" else "国际区"))
    print("密钥已保存到 %s（只有你的账户能读）。" % gclib.DEFAULT_KEY_FILE)
    print("现在可以回到智能体，告诉它“密钥设置好了”。")
    return 0


def cmd_check():
    key, src = gclib.resolve_key()
    if not key:
        print("NO_KEY：%s" % src)
        print("请用户本人在终端运行：python3 \"%s\" setup" % __file__)
        return 2
    try:
        host, tok = gclib.get_token()
    except gclib.GarminError as e:
        print("KEY_INVALID：%s" % e)
        print("请用户到佳明网页删除旧密钥、新建一个，再运行 setup。")
        return 1
    claims = gclib.jwt_claims(tok)
    region = "cn" if host.endswith(".cn") else "global"
    print("OK：密钥可用（来源：%s；地区：%s；令牌到期：%s）" % (src, "中国区" if region == "cn" else "国际区",
                                                     _expiry_text(claims.get("exp"))))
    scopes = claims.get("scope") or claims.get("scp") or claims.get("authorities")
    if scopes:
        print("权限：%s" % (" ".join(scopes) if isinstance(scopes, list) else scopes))
    try:
        acts = gclib.api_get("/activitylist-service/activities/search/activities", {"start": 0, "limit": 1})
        if acts:
            print("最近一次活动：%s" % acts[0].get("startTimeLocal", "?"))
        else:
            print("账号里暂时没有活动记录。")
    except gclib.GarminError as e:
        print("令牌可用，但读取活动失败：%s" % e)
        return 1
    return 0


def cmd_forget():
    for p in (gclib.DEFAULT_KEY_FILE, gclib.TOKEN_CACHE):
        try:
            p.unlink()
            print("已删除 %s" % p)
        except FileNotFoundError:
            pass
    print("本机已不再保存密钥。若要让密钥彻底失效，请到佳明网页“个人 API 密钥”页面点“删除”。")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    sys.exit({"setup": cmd_setup, "check": cmd_check, "forget": cmd_forget}.get(cmd, cmd_check)())
