# 接入佳明数据

## 目录
1. 个人 API 是什么
2. 引导用户获取密钥（可以直接照着说）
3. 保存与检查密钥
4. 常见问题
5. 能拿到哪些数据（接口与字段）
6. 可选：garmin_mcp（训练准备度、比赛预测、写入课表）
7. 隐私与使用条款

## 1. 个人 API 是什么

佳明在 2026 年开放了 Garmin Connect Personal API，让用户用自己生成的密钥读取自己的数据。它的特点：

- **只读**：只能 GET，不能写入或删除任何东西。
- **只能用于本人**：密钥只能用于你自己的数据和你自己运行的工具，不能分享或出售。官方 FAQ 写明它不适用于服务多个用户的商业应用。
- **调用方式**：先用密钥换取访问令牌，令牌有效 24 小时，没有刷新令牌，过期后再换一次即可。脚本会自动处理。
- **地区和主机**：
  - 国际区：`personal.connectapi.garmin.com`
  - 中国区：`personal.connectapi.garmin.cn`
  - 中国区密钥在国际区主机上会返回 404。`garmin_setup.py` 会依次尝试两个地区，自动判断。
- **官方参考**：
  - FAQ：https://developer.garmin.com/gc-developer-program/personal/personal-faq/
  - Python 客户端：https://github.com/garmin/connect-personal-api-python
  - 本技能的密钥文件位置 `~/.config/garmin/connect-personal-api-key` 与官方客户端相同，两者可以共用。

## 2. 引导用户获取密钥

可以把下面这段直接发给用户，按用户的账号地区保留对应的网址：

> 请在电脑浏览器里完成以下步骤，大约 2 分钟：
> 1. 登录网页版佳明 Connect。
>    - 国际区账号：https://connect.garmin.com/app/settings/personalApiToken
>    - 中国区账号（佳明中国 App / garmin.cn）：https://connect.garmin.cn/app/settings/personalApiToken
>
>    也可以从网页左下角“设置”进入，找到“个人 API 密钥”。
> 2. 点“创建密钥”，阅读并接受条款，选择有效期（30 天、90 天、180 天或 1 年）。打算长期使用的话，建议选 180 天或 1 年。
> 3. 点“创建”，然后复制密钥。**它只显示这一次**，丢了只能删掉重建。同一时间只能有一个密钥，新建前要先删掉旧的。
> 4. **不要把密钥发到聊天里。** 打开电脑上的“终端”：Mac 在启动台里搜索“终端”，Windows 打开 PowerShell。粘贴下面这行命令后回车：
>    `python3 "SKILL_DIR/scripts/garmin_setup.py" setup`
>    看到提示后粘贴密钥，屏幕上不会显示任何字符，这是正常的，直接回车。
> 5. 看到“成功”就回来告诉我。

注意事项：
- 发给用户前，把 `SKILL_DIR` 换成真实的绝对路径。
- Windows 上的命令可能是 `python` 或 `py`。
- 用户没有安装 Python 时，Mac 会提示安装命令行工具，按提示安装即可；Windows 可以从 python.org 安装。

## 3. 保存与检查密钥

| 命令 | 谁运行 | 作用 |
|---|---|---|
| `garmin_setup.py setup` | 用户本人，在终端里 | 隐藏输入密钥，测试连接，判断地区，保存密钥（权限 600） |
| `garmin_setup.py check` | 你（智能体） | 输出 `OK`/`NO_KEY`/`KEY_INVALID`、地区、令牌到期时间和最近一次活动日期，不显示密钥 |
| `garmin_setup.py forget` | 任何人 | 删除本机保存的密钥和令牌缓存 |

脚本按这个顺序找密钥：
1. 环境变量 `GARMIN_PERSONAL_API_KEY`
2. `--key-file` 指定的文件
3. `~/.config/garmin/connect-personal-api-key`

令牌缓存在 `~/.config/garmin-run-coach/token.json`，权限 600。

如果用户坚持把密钥贴进聊天：
1. 不要复述密钥。
2. 说明风险：它已经留在聊天记录里了。
3. 建议用户删掉这个密钥再新建一个，用 setup 输入新的。
4. 如果用户仍要继续，可以用不回显的方式把它写入密钥文件，并提醒用户之后换一个新密钥。

## 4. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `NO_KEY` | 还没运行 setup | 引导用户做第 2 节 |
| 两个地区都是 401/403 | 密钥过期、被删除或复制不完整 | 请用户重新创建密钥再 setup |
| 一个地区 404 | 地区不对 | 正常现象，脚本会自动试另一个地区 |
| 5xx、520 或超时 | 服务器暂时出错 | 脚本会自动重试；仍失败就过几分钟再运行，已下载的数据不会重复下载 |
| 中国区 `limit=50` 返回 500 | 中国区限制比文档更严 | 脚本每页取 20 条，健康数据按 28 天一个窗口拉取 |
| 健康数据为空 | 手表型号不支持，或没有佩戴睡觉 | 在报告里注明，相关规则不适用 |
| 密钥快到期 | 佳明会在到期前约一周发邮件提醒 | 到期后重新创建并运行 setup |

## 5. 能拿到哪些数据

`garmin_pull.py` 读取下面这些接口，数据保存在 `~/garmin-coach/raw/`：

| 数据 | 接口 | 保存位置 |
|---|---|---|
| 活动列表 | `/activitylist-service/activities/search/activities?start&limit&startDate&endDate` | `activities.json` |
| 单次活动详情（含主观用力 RPE 和感受） | `/activity-service/activity/{id}` | `details/{id}.json` |
| FIT 原始文件（逐秒心率、配速、步频、功率） | `/download-service/files/activity/{id}`，返回 ZIP | `fit/{id}.zip` |
| HRV（夜间均值、周均值、基线、状态） | `/hrv-service/hrv/daily/{开始}/{结束}` | `hrv/` |
| 睡眠（时长、分期、睡眠分、起床时间） | `/sleep-service/sleep/dailySleepSummariesByDate?startDate&endDate` | `sleep/` |
| 每日汇总（静息心率、压力、身体电量、步数） | `/usersummary-service/v2/dailysummaries?startDate&endDate` | `daily/` |
| 心率区间设置（最大心率、乳酸阈心率） | `/biometric-service/heartRateZones/v2` | `profile/hr_zones.json` |
| 用户设置 | `/userprofile-service/userprofile/settings` | `profile/user_settings.json` |

关键字段与单位：
- 活动列表里：
  - `distance` 单位是米，`movingDuration` 单位是秒，`averageSpeed` 单位是米/秒。
  - `averageHR`、`maxHR`：平均心率和最高心率。
  - `activityTrainingLoad`：佳明训练负荷，基于 EPOC 估算，属于厂商算法 [DEV-4]。
  - `hrTimeInZone_1..5`：按佳明区间设置统计，单位是秒。
  - `avgGradeAdjustedSpeed`：坡度校正后的速度。
  - `vO2MaxValue`：经常为空 [DEV-3]。
  - `eventType.typeKey == "race"`：用户把这次活动标记为比赛。
- 详情里：`summaryDTO.directWorkoutRpe` 是用户在手表上填的用力程度，取值 10–100，脚本除以 10 换成 1–10 分；`directWorkoutFeel` 是感受，取值 0–100。
- FIT 文件：`fitlite.py` 会读出逐秒的心率、距离、速度、步频、功率和海拔。默认丢弃 GPS 坐标。
- HRV：`lastNightAvg` 是夜间平均 RMSSD，单位 ms。`baseline.balancedLow/Upper` 是佳明的“平衡”区间。它和研究里的“晨起仰卧 5 分钟 RMSSD”不是同一种测量，规则套用时属于近似 [HRV-2]。
- 睡眠：
  - `sleepTimeSeconds`：睡眠时长。
  - `sleepScores.overall.value`：睡眠分。
  - `sleepEndTimestampLocal`：起床时间，毫秒时间戳，按本地时钟编码。
- 个人 API 拿不到训练准备度、训练状态、比赛预测、耐力分和爬坡分；也不能写入，所以课表不能通过个人 API 写进手表。

## 6. 可选：garmin_mcp

开源项目 https://github.com/Taxuspt/garmin_mcp（MIT 许可）把 python-garminconnect 包装成 MCP 服务器，能用在 Claude Desktop、Claude Code、Codex 等支持 MCP 的智能体里。它补上了个人 API 没有的两类能力：
- **读取**：训练准备度、训练状态、比赛预测、VO2max、乳酸阈、HRV、睡眠等。
- **写入**：上传训练课程（`upload_workout(s)`），排进日历（`schedule_workout(s)`），调整或撤销排课（`reschedule_workout`、`unschedule_workout`）。

安装要点（以它的 README 为准）：
1. 需要 Python 3.12+ 和 `uv`。
2. 用户本人在终端运行一次登录：
   ```bash
   uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
   ```
   - 中国区账号要加 `--is-cn`，或者先设置环境变量 `GARMIN_IS_CN=true`。
   - 程序会提示输入邮箱、密码和两步验证码。**这些必须由用户本人输入，你不能代填。**
   - 登录后令牌保存在 `~/.garminconnect`，权限只限本人，有效期约 6 个月。到期用 `--force-reauth` 重新登录。
3. 在智能体的 MCP 配置里加入 `garmin` 服务器：
   - 命令：`uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp`
   - 中国区在 `env` 里加 `GARMIN_IS_CN=true`
4. 可以用 `GARMIN_TOOL_PROFILE` 只开启需要的工具，比如 `workouts`、`training`；也可以用 `GARMIN_ENABLED_TOOLS` 设白名单，减少误操作。

使用提醒：
- 它用的是佳明网页的非官方接口，佳明改版时可能失效。
- 它有删除类工具，比如 `delete_activity`、`delete_workout`。**除非用户明确要求，否则不要调用任何删除工具。**
- 它返回的训练准备度、比赛预测都是佳明算法，没有独立验证 [PRED-8][DEV-3]。只能作为参考，不能替代本技能的多模型预测。

## 7. 隐私与使用条款

- 健康数据（心率、睡眠、HRV、轨迹）属于敏感个人信息，只保存在用户电脑上。`~/garmin-coach/` 目录权限是 700，里面有 `.gitignore` 防止误提交。
- 不要把原始数据、报告全文或轨迹发到任何网站。查文献时只搜索主题词，不要放用户数据。
- 个人 API 条款：密钥只能用于用户本人，以及用户自己开发或运行的工具。不能把别人的密钥收集到一个服务里统一使用；分享本技能时，每个人用自己的密钥。
