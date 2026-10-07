# 約每 5 分鐘的外部派發方案

本案選用 **cron-job.org 直接 POST** 作為主要觸發方式，設定步驟見 [cron-job.org 設定指南](cron-job-org.md)。下面保留共用 workflow 保護、故障分析與替代方式；Python helper／systemd 部署適用於另有自管主機的情況。

## 新證據與需求界線

截至 **2026-10-07 09:54 Asia/Taipei**，GitHub REST API 的 `event=schedule` 紀錄如下；三筆都是 `success`，`head_sha` 同為 `4e1aba6251b259a6a880071032bb84a6c6ce78e0`。

| Run | 建立時間（Asia/Taipei） | 與前一筆間隔 |
|---|---|---|
| [#8](https://github.com/dspp779/legacy-iow-volume/actions/runs/37487259982) | 2026-10-06 23:24:39 | — |
| [#9](https://github.com/dspp779/legacy-iow-volume/actions/runs/37526467018) | 2026-10-07 04:26:34 | 5:01:55 |
| [#10](https://github.com/dspp779/legacy-iow-volume/actions/runs/37549417999) | 2026-10-07 07:58:03 | 3:31:29 |

#10 到上述觀察時間已有 1:55:57 沒有新 run。Workflow API 同時顯示 `state=active`。因此「完全沒有 schedule event」已過時；可確認的是設定的每 5 分鐘沒有成為實際派發頻率。這些紀錄無法單獨證明 GitHub 內部延遲原因。[GitHub 官方事件文件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) 說明 schedule 可能延遲，負載高時可能丟棄排程工作。

本方案把需求定為：外部排程器每 5 分鐘嘗試派發一次。cron-job.org 的失敗／停用通知，加上查看 GitHub Actions 結果，是本案的日常操作起點。另提供可選 health check，以 15 分鐘內至少一筆近期建立且成功完成的 production run 作為初始告警門檻。外部派發消除了對 GitHub `schedule` 準時性的依賴，仍依賴 GitHub API、Actions queue、runner、舊平台與 IoW。若要求每筆上傳必須在 5 分鐘內完成，應改用自管短任務直接執行 uploader，並配置持久 state；Actions dispatch 無法提供這種期限保證。

上傳 workflow 現在只保留 `workflow_dispatch`，已移除原先的 GitHub schedule 備援。GitHub 文件規定公開 repo 60 天無活動會自動停用 scheduled workflows；本方案讓外部派發與這項 schedule 規則分離，無需依賴 dispatch 是否算作 repository activity。[GitHub 停用規則](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows) 原有 `LEGACY_IOW_SCHEDULE_BACKUP_ENABLED` 已不再使用。未來若需要 GitHub schedule 備援，必須放在獨立 workflow，另外評估停用、共享 concurrency/state 與派發權限；本 repo 目前沒有部署這項備援。

## 最小架構

```text
cron-job.org（每 5 分鐘）
  └─ POST /repos/dspp779/legacy-iow-volume/actions/workflows/legacy-iow-volume.yml/dispatches
       ref=main, dry_run=false, confirm_production=true, allow_empty_state=false
       └─ 固定 concurrency group: legacy-iow-volume
            └─ mode guard → restore/validate state → 舊平台讀值 → IoW
                 └─ 成功才更新 state → save cache → 確認新 cache key 存在

日常查看 → cron-job.org 呼叫紀錄 + GitHub Actions 執行結果
```

cron-job.org 只持有限定此 repo 的 GitHub dispatch token，不持有 `LEGACY_PASSWORD` 或 IoW OAuth secrets，也不跑 uploader。既有 Actions secrets、web 讀值與上傳程式沿用。若日後改用不休眠的自管維運主機，可使用 repo 的 Python helper／systemd 範例。

每個 tick 一次 POST，建議 20 秒網路逾時。HTTP 200（新 API 回傳 run id）或 204（舊回應形式）只代表接受派發，之後仍要查 completion。`tick_id` 只便於對照 log/run title，GitHub 不會以它去重；cron-job.org 使用每次替換的時間變數，自管 helper 使用 UTC 五分鐘 bucket。Helper 沒有自動 POST 重試。網路逾時可能已建立 run；不要當作「一定沒送出」立即連發，先看 Actions，或讓下一個 tick 再嘗試。401/403/404/422 要檢查 token、repo/ref、workflow/input；429/5xx 要監控 rate limit 或服務狀態。[Dispatch API 文件](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)

## Token 最小權限

| 用途 | 權限與存放 |
|---|---|
| 外部 fine-grained PAT | Resource owner `dspp779`，只選 `legacy-iow-volume`，repository **Actions: Read and write**；Metadata read 隨 token 取得。不要授予 Contents write、Workflows write、Administration 或 repo secrets 存取。設定期限與到期告警。 |
| GitHub App installation token（可替代 PAT） | App 僅裝在這個 repo，Actions write；短效 token 需另管理 App private key、installation id 與更新。 |
| Actions job 的 `GITHUB_TOKEN` | **Contents: read**，checkout 不持久化憑證。Cache action 使用 runner 的 cache token，本 job 不呼叫需要 Actions write 的 REST mutation。 |
| 獨立唯讀監控 | Actions read 即可；同一排程主機也可用現有 dispatch token 查 run。 |

外部 dispatch 最小權限依官方 [workflow endpoint 權限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。不要用 classic `repo` 大範圍 token。Actions write 是 repo 層級權限，不能只限制「這個 workflow 的 dry-run」，也能呼叫其他 Actions 寫入 API；因此 token 持有者是受信任的操作身分。`confirm_production` 只是防誤觸，並非秘密或身分隔離。其他敏感 workflows 與 main 的寫入權限仍須由 repo 管理管控。

## Dry-run 與 production guard

| 條件 | 結果 |
|---|---|
| Dispatch 省略輸入，或 `dry_run=true` | Dry-run；可登入舊平台、印出候選 payload，但不取 IoW token、不上傳、不寫 state、不 save cache。 |
| `dry_run=false`，未設 `confirm_production=true` | 失敗，讀資料之前停止。 |
| Repo variable `LEGACY_IOW_PRODUCTION_ENABLED` 不是字串 `true` | Production 失敗；dry-run 仍可用。 |
| 非本 repo、非 `refs/heads/main` | Job 略過；guard 也會拒絕。分支/tag/fork 不作 production 驗證。 |
| 非 `workflow_dispatch` 事件 | Guard 拒絕；workflow 沒有其他上傳 trigger。 |
| Production cache state 缺失或空物件 | 停止，要求恢復進度或人工確認一次性的 recovery。 |
| State JSON 或既有紀錄格式毀損 | 停止；`allow_empty_state` 不能繞過毀損。 |

IoW secrets 只在 mode guard 和 state guard 通過後的 production 上傳 step 提供。Repo production variable 控制 dispatch 的正式上傳；外部 cron 與 Actions 手動派發共用固定鎖。

這些是 workflow 的防誤操作保護。直接在其他主機執行 `legacy_iow_volume.py` 仍使用原本 CLI 規則，沒有自動套用 repo 開關或 Actions concurrency；不能同時部署另一個未共用鎖/state 的 production uploader。Main 被修改也能改掉 guard，需保護 main；若未來需要對 cron token 形成更強的權限邊界，可把 IoW secrets 移入限制分支的 production environment，另外評估審核等待是否符合自動排程。

## 自管主機替代方案的部署與啟用順序

使用 cron-job.org 時按 [設定指南](cron-job-org.md) 操作即可，以下不是本案的啟用前提。

本次只有 repo 變更與離線驗證，沒有安裝 timer、建立 PAT/App、設定 repo variables、dispatch 或 production 上傳。

1. 將 reviewed 變更合併到 `main`，離線 CI 通過。Production 開關尚未啟用時，production job 會停止，因此切換前先安排啟用窗口。
2. 在常駐 Linux 主機準備專用帳號 `legacy-iow-cron`，把 reviewed repo 版本放在 `/opt/legacy-iow-volume`，程式由管理者擁有且該帳號不可修改。主機使用 Python 3.9+；timer 本身只用標準函式庫。Uploader 沿用 GitHub Ubuntu runner 的 Python。
3. 建立只允許這個 repo、Actions write 的 token。用主機的 secret store 或 root 可讀的 `/etc/legacy-iow-dispatch.env` 注入：

   ```text
   LEGACY_IOW_GITHUB_TOKEN=<由秘密管理系統注入>
   LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED=false
   ```

   環境檔不可提交 repo、不可貼入 log，權限設 `0600 root:root`；system service manager 會在降權前讀取。不要把 token 放進 URL、命令列參數或 crontab。

4. 安裝 `deploy/legacy-iow-dispatch.service`、`.timer` 至 `/etc/systemd/system/`。先執行 `systemd-analyze verify` 與 `systemd-analyze calendar '*-*-* *:00/5:00 UTC'`，再 reload/enable timer。範例 service 預設派發 **dry-run**，不含 `--production`。若需要完全離線預覽，只執行：

   ```sh
   python3 scripts/dispatch_workflow.py --print-request
   ```

5. 維運啟用時，先讓外部 timer 連續派發 dry-run；觀察 UTC tick id、Actions 建立/開始/完成時間與 payload，確認至少多個五分鐘週期。Dry-run 仍會登入舊平台；本次不代為執行。
6. 查看既有 main state cache 尚在，無需更換 prefix 或清空 state。設定 repo variable `LEGACY_IOW_PRODUCTION_ENABLED=true`，環境檔設定 `LEGACY_IOW_DISPATCH_PRODUCTION_ENABLED=true`，然後用 service override 清除原 `ExecStart` 並換為：

   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/bin/python3 /opt/legacy-iow-volume/scripts/dispatch_workflow.py --production
   ```

   reload 後下一個 tick 才開始 production。**不要在常駐 service 加 `--allow-empty-state`**。
7. 在既有監控系統每五分鐘執行唯讀 `python3 scripts/dispatch_workflow.py --check-health --max-age-minutes 15`，非零 exit 告警。還要由主機以外的監控檢查 timer 主機存活及派發 heartbeat；與 cron 同機的 health check 無法發現整台主機離線。

Health check 查這個 workflow 最近 100 筆 main run，只計入 production `workflow_dispatch`，排除 dry-run 與所有歷史 schedule run。需有 15 分鐘內**建立**且成功完成的 production run，避免延遲數小時的 run 剛完成便被當作健康。超過門檻的 queued/running run 或最新 production failure 也會回報錯誤。若累積異常 dispatch 洪水超過 100 筆，檢查會偏向報無近期成功，需人工查完整歷史。

停用時先停止外部 timer，再把 repo `LEGACY_IOW_PRODUCTION_ENABLED=false`；保留 state。Repo variable 的更動不能撤回已通過 guard 的 run，停用當下仍須查看正在執行的工作。除非需要立即中止，讓它完成 state 保存，比在上傳中強制取消更容易保持一致。

systemd 的 `Persistent=true` 可在主機恢復後補觸發一次，並不補跑每一個錯過的 bucket；預設不疊加同一個正在執行的 service。Accuracy 設為 1 秒，沒有額外隨機延遲。[systemd timer 文件](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)

若用傳統 cron，使用相同專用帳號、由秘密管理系統注入環境，並以 Linux `flock -n` 包住相同程式，每五分鐘執行。例如 `*/5 * * * * /usr/bin/flock -n /run/legacy-iow-cron/dispatch.lock /usr/bin/python3 /opt/legacy-iow-volume/scripts/dispatch_workflow.py --production`。由管理者建立可供該帳號寫入的 lock 目錄。傳統 cron 不補跑停機期間的 tick，另接 log/告警與開機啟動驗證；不要同時啟用 cron 和 systemd timer。

## Concurrency 與 state：足夠的範圍

固定 `group: legacy-iow-volume`、`cancel-in-progress: false` 保證同一 repo 的這個 workflow 至多一班執行中。外部 cron 與手動 dispatch 共用，不按 event/ref 分組。預設最多一班 pending，新 pending 會取代舊 pending；這適用於本 uploader 每班讀取當下最新累計時數，不需執行每個錯過的 tick。它無法補齊舊平台沒有保存的歷史觀測，也不保證順序或五分鐘完成。不要改 `cancel-in-progress: true`，否則可能中止在「IoW 已收下、state 未存」之間。[官方 concurrency 行為](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

State 繼續使用 `legacy-iow-volume-` prefix，以相容既有 main cache。每台泵以 `pump_no` 比較 `seconds` 和 `received_at`；兩者相同即略過，任一改變則候選上傳，成功回應後才原子更新本地 state。只有 production 且 state 內容改變才建立新 cache，dry-run 和 no-op 不建立新的進度副本。保存後以同一精確 key 做 lookup，避免 save warning 被誤當成功；這只確認 cache entry 存在，並非驗證 IoW 接收的資料。[Cache action 行為](https://github.com/actions/cache)

對一般重複 tick、外部與手動派發重疊、成功後下一班恢復同一 state 的情況足夠；對以下情況不足以保證 exactly-once：

- Cache 可被刪除/淘汰，prefix restore 可能退回更舊版本。完全缺失現在會停止，但「舊 cache 還在」無法只靠現有 state 判斷新副本曾遺失。[GitHub cache 存取與淘汰規則](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
- IoW 收到 POST 但 client 逾時、runner 故障、寫 state 失敗或 cache save 失敗，下次可能重送。即使加持久 store，跨 IoW POST 與 state commit 仍有交易空窗；要做到 exactly-once 需 IoW 提供經驗證的 idempotency/upsert 規則。此 repo 沒有證據可宣稱相同 `Id + TimeStamp` 一定安全去重。
- 批次回應目前只檢查 HTTP 2xx，沒有逐筆核對；不能推論遠端部分成功或應用層錯誤的行為。`success` 也可能是所有泵都 skip/no-op，需獨立檢查資料新鮮度。
- State 不含 datastream/rate 身分。只改 `datastream_id` 或 `rated_cms`、讀值時數/時間沒變時會略過；需要事先規劃該泵的 state 遷移，不能清空整份 state 重新跑。多個 production 執行環境也不能只依靠這個 repo 的 concurrency。

因此本次選擇保留 cache 並補 guard/告警，作為最小改動。若重送不能接受或需要停機後保留完整 ledger，下一階段使用私有 object store/DB、版本或 CAS、共用鎖與可查驗的紀錄；IoW 去重契約仍需另外確認。不要把帳密、泵設定或未審查的 state 寫進此公開 repo 的 branch。

State 丟失時：先停止外部排程，確認無 active run；從可信保存副本恢復，或先核對 IoW 已接受的觀測。只有確認後才單次 `allow_empty_state=true` / helper `--production --allow-empty-state`。這會允許重送候選讀值，不能拿來驗證；本次沒有執行。毀損 JSON 必須先修復，恢復操作不會自動忽略它。後續定時觸發一律回到 `allow_empty_state=false`。

## 替代方案比較

| 方案 | 主要故障模式 | 維護成本與適用性 |
|---|---|---|
| **cron-job.org → workflow_dispatch（本方案）** | 服務延遲／停用、token 到期、API 故障、Actions 排隊、cache 遺失；HTTP 接受不等於執行成功 | 網頁設定一次 POST，token 輪替、失敗／停用通知與 Actions 結果查看；沿用 Actions secrets、log、短 runner。 |
| 自管 cron → workflow_dispatch | 主機離線、token 到期、API／Actions 故障 | 已有主機時可用 Python helper/systemd timer；需另維護主機與告警。 |
| GitHub schedule 作主路徑 | 延遲、丟班、60 天無活動停用；本次已觀察數小時間隔 | 成本低，沒有可接受的準時性證據；本方案已移除，若未來作備援需獨立 workflow，GitHub 故障時也無法接手。 |
| 長時間 GitHub-hosted job 內 `sleep 300` 迴圈 | job 被終止、runner 故障、部署需中斷；必須另設可靠重啟來源；睡眠耗 runner 時間 | 受 GitHub-hosted 每 job 6 小時上限限制，需要接棒與 state checkpoint，回到派發問題；維護較高。[Actions limits](https://docs.github.com/en/actions/reference/limits) |
| 常駐自管主機直接跑短 uploader + timer，或常駐 daemon | 主機/網路故障、OAuth、state 損毀；多 instance 需共用鎖；loop crash/drift 需 supervisor | 能避開 Actions queue；主機須保管舊平台與 IoW secrets、持久 state、備份、更新、log/告警。若五分鐘是完成期限，優先短任務 timer，而不是無 supervisor 的無限 loop。 |
| 外部 cron → self-hosted Actions runner | 仍依賴 GitHub API/queue/control plane，再加自管 runner 離線與更新 | 可減少 runner 配置不確定性，但增加主機與 runner 安全維運，未消除 GitHub 派發依賴。 |
| 外部 cron → repository_dispatch | 與 dispatch 相同的外部 cron/API/Actions 故障；只以 default branch 執行，payload 需自行驗證 | 新增 event_type/client_payload 與 workflow 路徑，fine-grained token 需 **Contents write**，比現有 workflow_dispatch 授權更廣，沒有時間可靠性優勢。[官方 endpoint](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event) |
| 託管外部 scheduler / function → workflow_dispatch | 供應商故障、IAM/secret/費用配置錯誤；自動重試可能重複觸發 | 可省主機維運，需處理供應商的 at-least-once delivery、DLQ/告警與費用；同樣共用 guard/concurrency/state。已有成熟平台時可替換本 timer。 |

## 離線驗證

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 scripts/dispatch_workflow.py --print-request
```

測試全域封鎖真實 urllib HTTP；GitHub 與 IoW 路徑只使用假 transport。涵蓋缺省/錯誤輸入、production 開關、repo/ref/event 拒絕、cache 缺失/毀損、API 200/204、401/403/429/5xx/逾時不重試、token 不輸出、唯讀 health、兩次相同讀值只送一次、dry-run 不改 state、失敗後不前移進度，以及「遠端已接受但 state 寫失敗」仍會重送的限制。獨立 `offline-tests.yml` 在 PR 與 main push 只執行這些測試，沒有 production secrets 與上傳 trigger。
