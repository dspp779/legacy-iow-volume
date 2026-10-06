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

排程每 5 分鐘一次，只在這個檔位於預設分支時才會跑。GitHub 可能延遲或略過整班。Actions 頁面可以手動執行；`dry_run` 預設勾選，只登入並讀資料，不上傳。排程沒有這個勾選，會真的上傳。

同一筆時數和更新時間不會重送。進度存在 Actions cache。
