"""claudework 测试副本：map_long0167 的生命周期校验上限。

原值 1200 秒 / 12000 步只是辅助函数的校验界：线上字段里姿态时钟按 3 秒取模、
prepared revision 按 u8 回绕，都不会因时长溢出。CLAUDEWORK_LONG=1 时放宽到 24 小时。
"""
import os

if os.environ.get("CLAUDEWORK_LONG") == "1":
    LIFE = float(os.environ.get("CLAUDEWORK_MAX_DURATION", "86400"))
else:
    LIFE = 1200
STEPS = int(LIFE * 10)
