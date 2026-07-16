# sync_automation

wsl网络不通原因：
问题就是 Docker 网段路由冲突——finkms.kingdee.com 解析到 172.18.64.149，被 Docker 的 172.18.0.0/16 路由劫持走了网桥，没走 Windows 主机。加了一条 /32 精确路由就解决了