# Ark Re:Code GVG Speed Tools

《星陨计划》（Ark Re:Code）团战/PVP 测速工具，提供三种使用方式：

| 目录 | 用途 |
| --- | --- |
| `Frida/` | 实时监听游戏数据并自动计算敌方速度 |
| `HoshinoBot/` | HoshinoBot 手动输入行动条的测速插件 |
| `Web/` | 浏览器中使用的手动测速页面 |

测速结果仅供个人娱乐和研究使用，不保证完全符合游戏内部实现。

## Frida 实时测速

实时版会监听游戏 WebSocket JSON 数据，从战斗开始信息计算我方实际面板速度，再根据开场与行动后的行动条差值反推敌方速度。

支持：

- GVG 上、下半场连续测速；
- PVP、普通战斗和深渊等带 `StartBattleInfo` 的战斗；
- 从行动包识别敌方角色；
- 优先使用不受速度群体潜能影响的我方角色作为参照；
- 游戏尚未启动时持续等待，打开游戏后自动附加。

### 文件说明

```text
Frida/
├─ speed.py          # 实时测速入口
├─ helper.py         # master 更新、数据读取和角色属性计算
├─ net_hook.py       # 可选：仅查看收发 JSON 数据
└─ data/
   ├─ dump.cs        # 用于动态解析 Hook RVA 和字段偏移
   └─ master.db      # 完整游戏 master 数据库
```

`master.json` 和 `config.json` 不是实时测速的必需文件。`helper.py` 直接读取完整的 `master.db`，不会把数据库裁剪成测速专用版本。

### 环境安装

建议使用较新的 Python 3：

```powershell
python -m pip install frida UnityPy
```

- `frida` 用于附加游戏进程；
- `UnityPy` 只在 `master.db` 缺失或需要更新时用于读取 bundle。

### 启动

在仓库根目录执行：

```powershell
python Frida\speed.py
```

默认寻找进程 `Ark ReCode.exe`。如果游戏还没有启动，程序会提示：

```text
找不到进程：Ark ReCode.exe，请先打开游戏，正在等待...
```

之后打开游戏即可自动继续。等待或测速期间按 `Ctrl+C` 可以停止。

程序每次启动都会：

1. 查询游戏 bulletin，检查最新 catalog；
2. 验证 `master.db` 是否存在、完整且为最新版本；
3. 必要时下载 `staticdata` 和 `text` bundle，原子重建完整数据库；
4. 从 `data/dump.cs` 动态解析发送/接收方法的 RVA 和帧字段偏移；
5. 附加游戏并等待战斗数据。

因此游戏 DLL 更新后，一般只需替换新的 `data/dump.cs`。如果相关类或方法被彻底改名，脚本会给出无法定位的错误，而不会继续使用旧地址。

### 常用参数

只检查 master 和 dump，不附加游戏：

```powershell
python Frida\speed.py --check
```

跳过在线 catalog 检查：

```powershell
python Frida\speed.py --offline
```

强制重新下载并构建完整 `master.db`：

```powershell
python Frida\speed.py --force-master --check
```

指定其他进程名或 PID：

```powershell
python Frida\speed.py --process "Ark ReCode.exe"
python Frida\speed.py --process 12345
```

也可以通过 `--dump` 和 `--master-db` 指定自定义数据文件路径。

### 仅监控游戏数据流

如果只想查看与原 `net_hook.js` 相同的收发 JSON：

```powershell
python Frida\net_hook.py
```

检查 `dump.cs` 是否能够解析：

```powershell
python Frida\net_hook.py --check-dump
```

## HoshinoBot 插件

将 `HoshinoBot/` 目录复制到 HoshinoBot 的模块目录，并命名为 `ark_recode_gvg_speedtest`：

```text
hoshino/modules/ark_recode_gvg_speedtest/
├─ __init__.py
├─ speed.py
└─ frame_buffer_ark.py
```

然后在 `config/__bot__.py` 的模块列表中加入：

```python
'ark_recode_gvg_speedtest'
```

重启 HoshinoBot 后，发送 `团战测速` 或 `帮助团战测速` 查看用法。

示例：

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

直接使用浏览器打开 `Web/gvg.html`，按照页面提示输入双方行动条和我方速度即可。此模式不需要 Frida、Python 或 HoshinoBot。

## 相关参考

- [Ark Re:Code Wiki](https://arkrecodewiki.miraheze.org/wiki)
- [异变的猫娘](https://space.bilibili.com/3546901544700020)的团战测速[教学视频](https://www.bilibili.com/video/BV1EcbRzGEz5)
- [HoshinoBot](https://github.com/Ice9Coffee/HoshinoBot)
