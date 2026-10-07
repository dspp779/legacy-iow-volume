# cron-job.org 每 5 分鐘啟動 workflow

主路徑是 `cron-job.org → GitHub workflow_dispatch → 舊平台讀值／IoW 上傳`。在 cron-job.org 的網頁設定一個 HTTP POST 即可，舊平台與 IoW secrets 繼續留在 GitHub Actions。

上傳 workflow 只接受 `workflow_dispatch`，不含 GitHub schedule，避免公開 repo 60 天無活動時 scheduled workflow 被自動停用。[GitHub 停用規則](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows) 移除 schedule 不影響以下 URL、token 或 request body；已啟用的 cron-job.org job 可以沿用。

## GitHub token

建立 fine-grained PAT，Resource owner 選 `dspp779`，Repository access 只選 `legacy-iow-volume`，Repository permissions 只加 **Actions: Read and write**。Metadata read 為隨附權限。不需要 Contents write、Workflows write 或把舊平台／IoW 帳密提供給 cron-job.org。[GitHub dispatch 權限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)

實際 token 只填在 cron-job.org 的 Authorization header，不貼進這個公開 repo。到期時替換該 header 的 token 即可。

## cron-job.org 設定

在 [Console](https://console.cron-job.org/) 新增 cron job，先保持停用，填入：

| 欄位 | 值 |
|---|---|
| 名稱 | `legacy-iow-volume 每 5 分鐘` |
| URL | `https://api.github.com/repos/dspp779/legacy-iow-volume/actions/workflows/legacy-iow-volume.yml/dispatches` |
| 執行頻率 | 每 5 分鐘；若選自訂時間，分鐘為 `0,5,10,15,20,25,30,35,40,45,50,55`，其餘全部 |
| 時區 | `Asia/Taipei` |
| HTTP method | `POST` |
| HTTP basic authentication | 關閉；使用下面的 header |
| Request timeout | 20 秒 |
| Notifications | 建議開啟失敗、失敗後恢復、被自動停用的通知 |

在進階設定加入以下 HTTP headers：

```text
Authorization: Bearer <只限這個 repo、Actions write 的 GitHub token>
Accept: application/vnd.github+json
Content-Type: application/json
X-GitHub-Api-Version: 2026-03-10
```

cron-job.org 支援 POST body 和自訂 headers；它會自行提供 User-Agent，因此不需要另外填。[服務 FAQ](https://cron-job.org/en/faq/)

Request body 初始使用 dry-run：

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": true,
    "confirm_production": false,
    "allow_empty_state": false,
    "tick_id": "cron-job.org-%cjo:unixtime%"
  }
}
```

`%cjo:unixtime%` 是 cron-job.org 在每次 HTTP 呼叫時替換的時間變數，方便在 Actions run title 對照是哪一次派發。[變數文件](https://docs.cron-job.org/creating-cron-jobs.html)

## 正式啟用

1. 可用上述 dry-run body 確認 cron-job.org 能建立 GitHub run，並在 Actions 確認結果。Dry-run 會登入舊平台讀值，但不呼叫 IoW、不推進去重 state。
2. GitHub repo 的 Settings → Secrets and variables → Actions → Variables，設定 `LEGACY_IOW_PRODUCTION_ENABLED=true`。查看既有 main state cache 仍在，沿用它。
3. 把 cron-job.org request body 的 `dry_run` 改為 `false`、`confirm_production` 改為 `true`，其他欄位保持原值，再啟用每五分鐘排程。**定期 job 的 `allow_empty_state` 永遠保持 `false`。**
4. `LEGACY_IOW_SCHEDULE_BACKUP_ENABLED` 已不再使用；若先前有設定，可保留或刪除，不影響派發。不要在這份上傳 workflow 加回 `schedule`。

直接 POST 不經過 repo 的 Python dispatch helper，所以也不需要 helper 的主機 production 環境變數。真正的上傳授權仍由 workflow 的 main 限制、`confirm_production`、repo production 開關與 state guard 判斷。

## 日常查看與停用

- cron-job.org 的成功只表示 GitHub 接受派發，HTTP 200 或 204 均可；實際 script 成敗看 [GitHub Actions](https://github.com/dspp779/legacy-iow-volume/actions)。保留 GitHub Actions 的失敗通知即可作為起點；repo 的 `--check-health` 程式是額外可選監控，並非啟動此 cron job 的前提。
- Token 到期通常會收到 401/403；workflow/ref/input 不符可能收到 404/422。修正後先查 Actions，避免把可能已接受的 timeout 當成必須立刻連發。
- 服務可能在連續失敗超過 25 次時自動停用 job；失敗／停用通知可幫助發現這種情況。[FAQ](https://cron-job.org/en/faq/)
- 停用時先關閉 cron-job.org job，再設 repo `LEGACY_IOW_PRODUCTION_ENABLED=false`。已通過 guard 的 run 仍可能正在完成上傳／保存 state。

服務 FAQ 目前表示不驗證目標 HTTPS 憑證；本方案沿用使用者選擇直接由 cron-job.org 呼叫，token 僅限這個 repo 的 Actions 權限。[來源](https://cron-job.org/en/faq/)

Concurrency、cache 去重的能力與限制，以及其他外部排程方式，見 [外部派發設計](external-cron.md)。
