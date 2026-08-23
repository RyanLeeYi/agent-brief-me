# Session handoff — F19 / F20（2026-08-23）

狀態：F19、F20 已簽核並 push（`74f1f00`），尚未動工。設計稿：https://claude.ai/code/artifact/3593be97-f431-4f2b-9c58-c5185fd61c0e

baton 五問已過（Outcome＝frozen acceptance；Independence＝不同函式區域；Ownership＝下表；Closure＝主 session merge + 全測 + acceptance-verifier）。上個 session 卡在 cwd 不是 git repo，worktree 開不出來，所以改在 repo 內的 session 派。

## 派工：兩個 `executor`，各自 `isolation:"worktree"`，同一輪排 `/loop` 保溫鬧鐘 2700s

共同段（兩張單都貼）：
- worktree 從 origin/main 開，HEAD 74f1f00 已含 F19/F20；**prompt 內 acceptance 原文是權威，直接照做**，feature_list.json 不必再查。
- 對外產物（html/py 註解、測試名）英文，不出現私人路徑。
- 同一阻塞失敗兩次就停下回報，不重試第三次。
- done：acceptance 全過、`python -m unittest discover tests` 全綠（playwright 缺席的 skip 是既有狀態）、commit 到 worktree 分支、不 push、不改 feature_list status。回報 commit hash、各條證據、跳過項。

### F19 單（acceptance 貼 feature_list.json F19 的 7 條原文）
- may-write：`scripts/brief_me.py` 只動 `report_view()` 與其新 helper（放 report_view 正上方）；`scripts/brief_me.html` 只動 `/* ---------- report card ---------- */` CSS 區與 `reportCardHTML()`；`tests/test_brief_me_api.py` 新測試加在**檔尾 class 最後**；`tests/test_brief_me_html.py` 新測試加在檔尾。
- must-not-write：`sessions_view()`、`_project_current`、任何 `session*` 函式／CSS、`:root` 變數表、feature_list.json、docs/。
- commit message：`feat: report card three-part summary with feature chip (F19)`
- 原因：report 只顯示單行截斷，看不出 feature 終態與卡點；agents.md 規定 summary 三段，UI 要結構化顯示。

### F20 單（acceptance 貼 feature_list.json F20 的 9 條原文）
- may-write：`scripts/brief_me.py` 只動 `sessions_view()`、`_project_current` 與新 helper（放 sessions_view 正上方）；`scripts/brief_me.html` 只動 `/* ---------- sessions view ---------- */` CSS 區、`sessionRunningCardHTML`／`sessionFinishedRowHTML`／`renderSessions`、sessions 的 section-header markup，以及 `:root` 新增一行 `--danger-bg`；`tests/test_brief_me_api.py` 新測試**緊接在 `test_state_sessions_ended_by_report` 之後**（不要加在檔尾，F19 會加那裡）；`tests/test_brief_me_html.py` 新測試加在現有 sessions 測試之後。
- must-not-write：`report_view()`、`reportCardHTML()`、report card CSS、feature_list.json、docs/。「open report」只需切 view 並展開 `data-id` 符合的 `.report-card`，不要改它的 markup。
- commit message：`feat: sessions table with outcome badges and running progress (F20)`
- 原因：exit 4294967295 直接印在畫面上沒人看得懂；08/22 有 4 個 headless session 被砍卻顯示成普通結束，分不出「做完」與「被殺」。

## 整合（主 session）
1. 兩個分支 merge 回 main（預期零衝突；有衝突就是 ownership 越界，回頭看誰寫錯區域）。
2. `python -m unittest discover tests`。
3. 派 `acceptance-verifier` 逐條驗 F19、F20。
4. passing → 整條原文搬 `docs/archive/features.jsonl`，主檔只留 failing。
