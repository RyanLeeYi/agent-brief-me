# agent-brief-me

Claude Code plugin：agent 的非同步決策收件匣。工作中的 session 把「要使用者決定的問題」與「收工報告」投進共用 JSONL 收件匣；使用者在睡前／早上兩個審核站用 `/brief` 批次審核並派出 headless session 繼續工作。

## 規格權威

`feature_list.json` 的 `acceptance` 是唯一權威，逐條可判定，不引用外部規格文件。`signed_off: true` 之前不動工。

## 開發約定

- 開源 repo：對外產物（README、docs/、skills/、scripts/ 內容）一律英文；feature_list.json 與本檔用繁體中文。不得出現作者私人路徑或 repo 名（F6 有 grep 檢查）。
- 資料檔（`~/.agent-brief/`）永遠 append-only，不重寫既有行。
- 驗證方式：每條 feature 的 acceptance 自帶測法；併發寫入有 `tests/test_concurrent_append.py`。
- headless 派 subagent 的可行性證據：見 `docs/headless-delegation-evidence.md`（F5 建立）。

## 環境

無外部依賴；Python 僅用於測試與 dispatch 腳本（stdlib only）。冒煙：`python tests/test_concurrent_append.py`。
