# Ark Re:Code GVG Speed Calculator (HoshinoBot Plugin)

一个用于**HoshinoBot**的星陨计划（Ark Re:Code）团战（GVG）速度计算插件。  

使用Monte Carlo，根据我方乱速，终点行动条和速度来计算敌方速度区间、均值、中位数以及稳定超车所需的最低速度。  

由于本人对游戏机制也仅是一知半解，行动条相关均视为四舍五入（这点是通过行动条可以并列100，且乱速可能出现0或5推断）。

对计算结果正确性不做保证，**仅供个人娱乐用途**。

## 安装方法

1. 在HoshinoBot的插件目录HoshinoBot/hoshino/modules下clone本项目

   `git clone https://github.com/jajajag/ark_recode_gvg_speedtest`
2. 在config/\_\_bot\_\_.py的模块列表里加入ark_recode_gvg_speedtest
3. 重启HoshinoBot

## 使用方法

发送`团战测速`或`帮助团战测速`查看具体用法。

以下是目前支持的指令：

```
团战测速
水马 1 56 135
水琴 1 70 170
水拳 4 58 131
朱茵 1 101
盖儿 1 84
```

【团战测速】后每行依次是角色/乱速/终点行动条/(速度)，其中有速度的为我方，没有速度的为敌方，101表示第一个行动。其中换行可替换为空格。

```
乱速 260 265
```

【乱速】计算两个角色乱速的百分比概率。

```
python frame_buffer_ark.py
```

【卡帧】原本是pcr用的mumu卡帧脚本，修改一下可以在星陨计划里用来卡帧刷真实乱速。

## 相关参考

- [异变的猫娘](https://space.bilibili.com/3546901544700020)的团战测速[教学视频](https://www.bilibili.com/video/BV1EcbRzGEz5)。

- [HoshinoBot](https://github.com/Ice9Coffee/HoshinoBot)
