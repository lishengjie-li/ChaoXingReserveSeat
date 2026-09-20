无法预约情况debug方式
> 1、电脑端访问："https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid=" 使用自己的用户名密码登录
> 2、电脑端访问：”https://office.chaoxing.com/front/third/apps/seat/code?id={图书馆id}&seatNum={座位id}“查看是否显示时间表
> 3、尝试预约看看是否会出现验证方式

目前无法实现跨单位座位预约，仅可以预约同一阅览区的不同座位。

---

## ⚠️ 本通道【不做签到】

GitHub Actions 抢座成功后，**不会**自动签到。原因：签到调度依赖本机 `at` 定时任务，
而 runner 用完即焚，11 小时后的 `at` job 会随虚拟机一起消失。

**抢到座位后请自行到馆签到**，否则会记违约：

| 项 | 值 |
|---|---|
| 签到窗口 | 时段开始后 **30 分钟**内 |
| 违约累计 | 3 次 → 限制预约 7 天 |
| 限制原因 | 由图书馆管理员**人工审核**（App 显示"非法预约"） |

## 运行方式

| 通道 | 触发 | 说明 |
|---|---|---|
| 定时 | 每天 UTC 11:45（北京 **19:45**） | 自动跑，20:00 放票前完成登录预热 |
| 手动 | Actions → auto_Reserve → Run workflow | 可选 `profile`（human=首击 8.8s / fast=首击 4.0s）与 `offset` |
| 预检 | Actions → preflight_test → Run workflow | 只登录 + 查座位是否已有预约，**不下单** |

## Secrets 配置

需在仓库 Settings → Secrets and variables → Actions 里配置：

- `USERNAMES`：账号，多个用英文逗号分隔
- `PASSWORDS`：密码，多个用英文逗号分隔（**顺序必须与 USERNAMES 一一对应**）

## 关于首击时刻（8.8s）

`main.py` 的 `HUMAN_PROFILE = "human"` 会把首个请求安排在放票后 **8.8 秒**发出
（`fast` 档为 4.0 秒）。这是**拟人化与成功率的硬 trade-off**：

- 越靠近 0 秒 → 越容易被识别为脚本，但抢到热门座位概率高
- 8.8 秒 → 更像真人操作，但热门座位大概率已被抢走

实时生效值 = `PACED_FIRE_OFFSET` − `INNER_JITTER_MEDIAN` + `reserve.py` 的随机抖动(0~0.2s)，
补偿后分布中心落在 8.8s（实测 min 8.373 / max 9.225 / mean 8.809）。


