"""钉钉远程驱动 Claude 旁路桥。

独立守护进程：dingtalk-stream 长连接收消息 → 常驻 claude(stream-json) 作答 → @回复。
与 server/trading/caisen 完全解耦，互不影响进程命运。
"""
