# 把课表写进佳明并同步到手表

目标：用户打开手表，在今天的日期上就能看到这节课；开始跑步后，手表会按步骤提示心率或配速。

## 目录
1. 准备：生成导入文件
2. 路线 A：浏览器自动写入（推荐，已在中国区实测）
3. 路线 B：garmin_mcp 自动写入
4. 路线 C：手动建课
5. 写完后的核对
6. 同步到手表
7. 之后更新课表
8. 安全规则与常见问题

## 1. 准备：生成导入文件

```bash
python3 SKILL_DIR/scripts/plan_to_garmin.py --weeks 1-8      # 或 --from 2026-11-05 --to 2026-12-31
```

- **一次只写 4–8 周**，写到下一个检查点为止。检查点之后配速可能要更新，写太远会留下一堆过时的课。
- 输出在 `~/garmin-coach/plan/garmin/`：
  - `workouts.json`：每种课一个佳明训练课程。同名的课只建一次，不同日期重复使用。
  - `schedule.json`：每次排课的日期和课程名。
  - `connect_upload.js`：路线 A 的预览脚本，只读不写。
  - `connect_upload_write.js`：路线 A 的写入脚本。
  - `mcp_upload.json`：路线 B 的上传参数。
  - `manual.md`：路线 C 的手动清单。
- 课程名都带同一个前缀（`plan.json` 里的 `meta.name_prefix`，例如“春马·”），方便用户在佳明里认出这些课，也方便脚本判断哪些已经建过。

格式说明：脚本生成的是佳明 workout-service 的 JSON。
- 步骤类型：热身、训练、恢复、放松、重复组。
- 结束条件：距离（米）、时间（秒）或按圈键。
- 目标：
  - 心率目标的 `targetValueOne` 是下限、`targetValueTwo` 是上限，单位 bpm。
  - 配速目标的单位是米/秒，`targetValueOne` 放较快的速度。这一格式已在中国区网页写入并显示正确。

## 2. 路线 A：浏览器自动写入

条件：
- 你能控制浏览器并在页面里执行 JavaScript，例如 Claude in Chrome 的 javascript 工具。
- 用户已经在这个浏览器里登录了 Connect：
  - 国际区：`connect.garmin.com`
  - 中国区：`connect.garmin.cn`

**登录必须由用户本人完成，你不能输入账号密码。**

步骤：
1. 在浏览器里打开 `https://connect.garmin.cn/modern/` 或 `https://connect.garmin.com/modern/`，确认已经登录，页面没有跳到登录页。
2. **预览**：读取 `connect_upload.js` 的全部内容，作为 JavaScript 在这个页面里执行。它只读不写，会返回：
   - `toCreate`：要新建的课程名
   - `reuse`：可以复用的已有课程数
   - `toSchedule`：要排入的次数
   - `alreadyScheduled`：已经排过的次数
   - `otherWorkoutsOnSameDays`：同一天已有的其他课
   - `trainingPlans`：进行中的佳明教练或训练计划
   - `fail`：失败项
3. **把预览结果用一句话告诉用户**，等用户明确同意。例如：“将新建 18 种课程，排入 30 次（12/7–1/31）；1/9 已经有一节佳明教练的课；你的账号里有一个进行中的佳明教练计划。要继续写入吗？”
   - 同一天已经有别的课：问用户保留哪个。**不要删除用户原有的内容。**
   - 有进行中的佳明教练自适应计划：它每天会往手表推自己的课，和这份课表冲突。是否退出、怎么退出，交给用户决定：退出路径是网页“训练和计划 → 训练计划”。如果用户要求你代为操作，要先截图确认页面，再按用户的指示做。
4. **写入**：用户同意后，执行 `connect_upload_write.js`。它会：
   1. 按名字复用已有课程，没有的才新建；
   2. 跳过已经排在同一天、同名的课；
   3. 每次请求间隔 0.3 秒；
   4. 返回 `created`、`scheduled` 和 `fail`。
5. 脚本可以重复运行。中途失败时，再运行一次只会补上缺的部分。

技术细节（排查问题时用）：
- 脚本调用的是网页自己使用的 `/gc-api/` 接口，带上页面 `<meta name="csrf-token">` 里的值作为请求头 `connect-csrf-token`，并设置 `credentials: 'include'`。
- 用到的接口：
  - `GET /gc-api/workout-service/workouts?start=1&limit=100&myWorkoutsOnly=true…`
  - `POST /gc-api/workout-service/workout`
  - `POST /gc-api/workout-service/schedule/{workoutId}`，请求体为 `{"date":"YYYY-MM-DD"}`
  - `GET /gc-api/calendar-service/year/{年}/month/{月-1}`：月份从 0 开始；相邻月份的数据会重叠，所以按 id 去重。
- 这些是非官方接口，佳明网页改版时可能失效。失效时退回路线 B 或 C。
- 这条路线已在中国区网页实测；国际区的网页前端是同一套，路径应该相同，但没有实测过。

## 3. 路线 B：garmin_mcp 自动写入

条件：已按 `connect-garmin.md` 第 6 节装好 garmin_mcp，用户本人运行过 `garmin-mcp-auth`。中国区账号要设置 `GARMIN_IS_CN=true`。

步骤：
1. **检查冲突**：调用 `get_scheduled_workouts(start_date, end_date)` 和 `get_training_plan_workouts`（或 `get_garmin_coach_workouts`），看看要写入的这段日期里已经有什么。把冲突告诉用户，等用户同意后再继续。
2. **上传课程**：调用 `get_workouts` 列出已有课程，然后对 `mcp_upload.json` 里还没有的课程（按名字判断）调用 `upload_workouts(workouts=[...])`。
3. **整理 ID**：把返回结果里每个课程的 `workout_id` 和已有课程的 ID 合在一起，存成 `{课程名: id}` 格式的 JSON，比如 `ids.json`，然后运行：
   ```bash
   python3 SKILL_DIR/scripts/plan_to_garmin.py --mcp-ids ids.json
   ```
   这一步会生成 `mcp_schedule.json`。
4. **排课**：调用 `schedule_workouts(schedules=<mcp_schedule.json 的内容>)`。它会跳过同一天已经排过的同一课程。
5. 写错了就用 `unschedule_workout` 撤销。**不要调用任何 delete 工具**，除非用户明确要求删除某个具体的课程。

## 4. 路线 C：手动建课

把 `manual.md` 发给用户，或者整理成清单，告诉用户：
1. 打开佳明 Connect App，进入“训练与计划”→“训练课程”→“创建训练”→“跑步”。不同版本的菜单名称可能略有不同。
2. 按清单逐步添加。
   - 每一步的类型：热身、跑步、恢复、放松，或者重复。
   - 结束方式：距离或时间。
   - 目标：心率或配速范围。
3. 保存后，点“添加到日历”或“排程”，选择清单里的日期。
4. 一种课只需要建一次，以后在不同日期重复添加。

## 5. 写完后的核对

- 路线 A：再运行一次 `connect_upload.js` 预览。`alreadyScheduled` 应该等于计划里的排课数，`toCreate` 应该是空的。
- 路线 B：用 `get_scheduled_workouts` 按日期列出排课，和 `schedule.json` 逐条对照。
- 每天只能有这份课表里的一节课，除非用户自己保留了别的课。
- 在网页日历里打开一节课，检查心率或配速目标是否显示正确，比如阈值课的配速是 5:00–5:06，不是反过来。
- 给用户一个简短的汇总：写入了哪几周、多少节课、第一节课是哪天。

## 6. 同步到手表

告诉用户：
1. 打开手机上的佳明 Connect App，下拉同步，或者等它自动同步。
2. 在手表上进入“训练 → 训练日历”，就能看到排好的课；有些型号在开始跑步前会提示“今日训练”。不同型号的菜单名称不同，比如“训练”“训练课程”“日历”。
3. 跑步时选择这节课，手表会提示每一步，偏离心率或配速范围时会提醒。
4. 如果账号里还有佳明教练计划，或者打开了每日建议训练，手表也会推它的建议。告诉用户：以这份课表为准，每日建议只作参考。

## 7. 之后更新课表

- **检查点之后**：更新配速，生成下一段课表，再用同样的方式写入。
  - 前缀要相同，但如果课程内容变了，名字也要变（比如“阈值 3×10′（新配速）”），否则会复用旧课程，目标还是旧的。
  - 旧的、还没跑的排课，只有在用户同意后，才用 `unschedule` 或网页日历撤销。
- **临时改某一天**：按“只改今天”的规则，用户在跑步时手动选轻松跑就可以，不需要改日历。

## 8. 安全规则与常见问题

- 写入前先预览，等用户同意；不删除用户原有的内容；不代替用户退出计划；不代替用户输入密码。
- 返回 401 或 403：登录过期。请用户在浏览器里重新登录，你再重试。
- 页面没有 `csrf-token`：可能还在加载，或者已经跳到登录页。刷新后先截图看看。
- 返回 429：请求太快。等几分钟再运行，脚本会跳过已经完成的部分。
- 网页结构变了，导致接口返回 404：退回路线 B 或 C，并把报错告诉用户。
- 手表上看不到课：确认是同一个账号，手动同步一次；确认日期在手表日历的显示范围内；有些旧型号不支持日历里的结构化课程，需要在 App 里对这节课点“发送到设备”。
