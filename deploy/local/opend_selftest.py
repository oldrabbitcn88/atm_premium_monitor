# -*- coding: utf-8 -*-
"""OpenD 自检：验证能连上并且已登录（端口通不代表登录成功）"""
import socket
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(BASE, "..", "..")))

from backend.data.futu_adapter import FutuAdapter  # noqa: E402


def port_open(host='127.0.0.1', port=11111, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    if not port_open():
        print('[selftest] 端口 11111 未监听——OpenD 没起来，或起来后因登录失败又关掉了')
        return 1

    f = FutuAdapter()
    try:
        price = f.get_price("HK.800000")
        if not price:
            print("[selftest] 连上了但取不到报价——通常是未登录或行情权限问题")
            return 1
        print(f"[selftest] 恒指最新价: {price}")
        print(f"[selftest] 下一个月度交割日: {f.get_hsi_monthly_expiry_next()}")
        print("[selftest] OK")
        return 0
    finally:
        f.close()


if __name__ == "__main__":
    sys.exit(main())
