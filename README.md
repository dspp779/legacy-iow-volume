# legacy-iow-volume

把舊平台 4G 累計運轉時間換成立方公尺，上傳到 IoW。這個倉庫是公開的，GitHub 託管 runner 的排程不計入私有倉庫分鐘數。

帳號、密碼、OAuth 和泵設定都放在 repository secrets，不要提交。

## Secrets

| 名稱 | 內容 |
|---|---|
| `LEGACY_UNIT` | 舊平台登入單位 |
| `LEGACY_ACCOUNT` | 帳號 |
| `LEGACY_PASSWORD` | 密碼 |
| `LEGACY_IOW_PUMPS_JSON` | 泵設定，整份 JSON |
| `IOW_CLIENT_ID` | IoW OAuth client id |
| `IOW_CLIENT_SECRET` | IoW OAuth client secret |

## 泵設定

以 `pumps.example.json` 為底，`source` 必須是 `web`。網頁模式用 `pump_no` 對即時清單。每台的 `datastream_id` 填 IoW「累積總抽水量」的 UUID，空白的那台會略過。`rated_cms` 省略時是 `0.3`。

把完成的 JSON 整份貼進 `LEGACY_IOW_PUMPS_JSON`。

## 執行

主路徑使用 **cron-job.org 每 5 分鐘直接 POST GitHub `workflow_dispatch`**。可照 [cron-job.org 設定指南](docs/cron-job-org.md) 填 URL、headers 和 request body；上傳程式與既有 secrets 繼續由 Actions 執行。Actions 排隊仍可能延遲，因此這是約每 5 分鐘派發的方案。其他外部 cron、concurrency/state 限制與故障比較見 [外部派發設計](docs/external-cron.md)。

Actions 手動執行預設 `dry_run=true`，只登入並讀資料，不上傳、不修改 state，也不提供 IoW OAuth secrets。Production 需 `dry_run=false`、`confirm_production=true`、repo variable `LEGACY_IOW_PRODUCTION_ENABLED=true`，且只能在這個 repo 的 `main` 執行。

原有 `*/5 * * * *` GitHub schedule 保留為備援，預設關閉；另設 `LEGACY_IOW_SCHEDULE_BACKUP_ENABLED=true` 才會執行，也受 production 開關與 state 檢查保護。兩條路徑共用固定 concurrency group，不取消進行中的上傳。

成功上傳後才更新去重 state；同一筆時數和更新時間通常會略過。進度沿用 Actions cache，缺失或空白時 production 預設停止。Cache 不是持久交易紀錄，仍有上傳成功但 state 保存失敗／cache 回退時重送的風險，詳見操作手冊。
