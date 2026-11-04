"""多个客户端消费者共用的 WAD 后缀和 LCU 虚拟条目前缀。"""

WAD_SUFFIX = ".wad.client"
# WAD 内部条目不随磁盘上是否包含 LeagueClient 目录而改变。
RCP_GLOBAL_PREFIX = "plugins/rcp-be-lol-game-data/global"
