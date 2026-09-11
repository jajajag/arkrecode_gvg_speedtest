# Ark Re:Code GVG Speed Tools

《星陨计划》（Ark Re:Code）团战测速工具，目前支持三种测速方式：

| 目录 | 用途 |
| --- | --- |
| `Frida/` | 实时监听游戏数据并自动计算敌方速度 |
| `HoshinoBot/` | QQ 机器人手动输入行动条的测速插件 |
| `Web/` | 浏览器中使用的手动测速页面 |

测速结果仅供**个人娱乐和研究使用**，不保证完全符合游戏内部实现。

## Frida 实时测速

实时版会监听游戏通信数据，从战斗开始信息计算我方实际面板速度，再根据开场与行动后的行动条差值反推敌方速度。

**注意：程序会对游戏客户端进行注入，请谨慎使用！！！**

目前支持（**保证公平性，不会获得比游戏内录屏更多的信息**）：

- GVG、JJC 和深渊的实时测速（其他场景未测试）；
- 开局通过解包数据计算角色面板速度，再通过行动条计算敌方速度区间和速度期望；
- 优先使用不受速度阵影响的我方角色作为参照（你游速度阵疑似有 bug？）；
- 仅当敌方角色出手和羁绊触发时，显示敌方当前最大生命值和羁绊信息。

### 环境安装

安装 frida 和 UnityPy：

```
pip install frida UnityPy
```

### 启动

在仓库根目录执行（或者下载最新的 Release 打开 `speed.exe`）：

```
python Frida\speed.py
```

程序每次启动都会检查最新的 catalog 来验证 `master.db` 是否存在且为最新版本，不是最新则会自动下载解包数据更新。用于注入的方法 RVA 和偏移量是从 `data/dump.cs` 解析，如果游戏的 `GameAssembly.dll` 更新，则需要重新用 `Il2CppDumper` 来生成替换新的 `data/dump.cs`。

### 打包

```
pyinstaller -F Frida/speed.py --collect-all frida --collect-all UnityPy --clean --noconfirm --icon=Frida/data/icon.ico
```

## HoshinoBot 插件

### 安装

1. 在HoshinoBot的插件目录modules下clone本项目

```
git clone https://github.com/jajajag/arkrecode_gvg_speedtest
```

2. 在 `config/__bot__.py` 的模块列表中加入 `ark_recode_gvg_speedtest`

3. 安装 UnityPy

```
pip install requests UnityPy
```

4. 复制 `HoshinoBot/data/account_example.json` 为 `HoshinoBot/data/account.json`，在同一文件的 `MainAccount`、`SubAccount` 中填写大号、小号的 Token 和 GID。通用日常选项放在顶层，账号内可覆盖同名选项；刷新 Token 只更新对应账号。旧版顶层 `Token` / `GuildID` 仍作为大号读取，只需补充 `SubAccount`。

5. 重启 HoshinoBot 后，发送 `团战测速` 或 `帮助团战测速` 查看用法。

### 可用指令

团战示例：

```text
团战 作业 角色1 角色2 角色3
团战 胜率表
团战 数据 玩家名或CUID
团战 一速 玩家名或UID 速度
团战 信息 玩家名或UID 内容或图片
团战 历史 玩家名或UID
团战 玩家名或UID
团战 清日常（仅限Bot主）
团战 更新数据（仅限Bot主）
```

团战测速示例：

```text
团战测速
水马 1 56 135
水琴 1 70 170
水拳 4 58 131
朱茵 1 101
盖儿 1 84
```

每行依次为“角色、开场乱速、终点行动条、速度”。带速度的是我方角色，不带速度的是敌方角色；`101` 表示第一个行动。

团战总结示例：

```text
团战总结
上路上半
水马 1 56 135
水琴 1 70 170
水拳 4 58 131
朱茵 1 101 专武1.3w
盖儿 1 84 闪避羁绊
```

乱速概率：

```text
乱速 260 265
```

## Web 手动测速

直接使用浏览器打开 `Web/gvg.html`，按照页面提示输入双方行动条和我方速度即可。

如果不想本地运行，也可以直接使用 [在线演示](https://ark.jajajag.com/) 版本。

简化自 [zzasqas/ArkRecodetools](https://github.com/zzasqas/ArkRecodetools/blob/main/guild-battle.html)。

## 相关参考

- [Ark Re:Code Wiki](https://arkrecodewiki.miraheze.org/wiki)
- [异变的猫娘](https://space.bilibili.com/3546901544700020) 的团战测速 [教学视频](https://www.bilibili.com/video/BV1EcbRzGEz5)
- [HoshinoBot](https://github.com/Ice9Coffee/HoshinoBot)
- [ArkRecodetools](https://github.com/zzasqas/ArkRecodetools/blob/main/guild-battle.html)
- [openrubi](https://github.com/StardustChocolate/openrubi) 的角色别名表

### 每日采集流程

每天北京时间 08:01（UTC 00:01）依次执行大号日常、大号完整团战数据查询、小号日常。
以本次完整团战响应中的敌方防守名单判断是否开战，不使用固定星期判断。
有敌方防守时保存名单及原始防守数据，再用小号逐人采集竞技场防守和助战装备；
没有敌方防守时清空当前对手名单，使用小号采集排行榜前 20 团的攻防记录。
响应缺失或格式异常会报错，不当作休战；历史防守、装备按 UTC 日期保留。

`团战 数据 xxx` 优先精确匹配名字，再匹配 CUID，最后模糊匹配名字；同名提示 CUID 和头像角色。
输出上、下半角色、生命、套装、羁绊及采集日期。理论配装与 `pvp_speed.py` 一致：
只取武器、头、衣、项链、戒指的速度副词条；散件基准 169，五件中至少三件速度套时基准 189。
先选一速，再排除已用装备选二速，并输出各部位套装和速度。汇总历史采集装备，同一件装备只保留最新属性。
这些结果是兔子基准的已知装备组合，不代表实际团战速度。
`团战 更新数据` 执行双账号采集但不清日常，`团战 清日常` 依次清理两个账号。

### 插件代码结构

- `gvg.py`：机器人指令、定时任务入口。
- `updater.py`：双账号执行顺序、团战及装备采集。
- `daily.py`：日常清理。
- `queries.py`：玩家匹配、统计查询、防守展示和理论配装。
- `database.py`：建表、迁移、名单和快照存储。
- `api.py`：账号配置、会话和游戏请求；`master.py`：静态数据更新。
- `speed.py`：原有行动条测速算法。`frame_buffer_ark.py` 是独立的 Windows 逐帧工具，不参与机器人运行。

防守属性计算复用 `Frida/helper.py`，不复制第二套属性公式。
