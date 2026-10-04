# P1 API contract

> 说明：本文件是早期 P1 阶段的接口摘要，仅作背景；当前接口以 `docs/openapi.json`（由应用生成，`./iirp check` 校验一致）为准。

Same-origin JSON. /api/v1 prefix. POST/PATCH accept Content-Type application/json. All mutations require header X-IIRP-Client: web (CLI may use cli), Origin if present must match Host. Errors {detail: human readable string}. API never waits for external providers.

GET /home -> {data_status:"NOT_FETCHED", market:[{symbol,name,value:null,change_percent:null,as_of:null,status:"NOT_FETCHED"}], feed:[], policy:Policy, active_jobs:number, worker:Worker, mode:"development", notice:string}
GET /feed -> {data_status:"NOT_FETCHED",groups:[],coverage:{status:"NOT_FETCHED",message:string}}
GET /demo -> {label:"合成测试样例 · 非真实交易", groups:[{id,company,ticker,accepted_at,transaction_dates,owners:number,filings:number,transactions:[{id,owner,code,kind,shares,price,transaction_date,accepted_at}]}]}
GET /jobs -> {items:Job[],worker:Worker}; newest first, <=100.
POST /collections {kind:"fixture_check"|"market_probe"|"sec_probe", ticker?:"AAPL"} -> 202 {job_id:string,reused:boolean,job:Job}
POST /jobs/:id/actions {action:"pause"|"resume"|"cancel"|"retry"} -> Job
GET /collection-policy -> Policy
PATCH /collection-policy {sec_enabled:boolean} -> Policy. P1 automatic mode only schedules bounded SEC capability probes; no historical collection. SEC config required for enable.
GET /providers -> {items:[{id,name,configured:boolean,status,message,budget:string}]}
GET /coverage -> {items:[{provider,target,status,message,updated_at,source_hash?:string}],notice:string}
GET /system -> {worker:Worker,mode,version,storage:{free_bytes,objects,bytes},migration,automatic_collection_scope:string}
GET /search?q= -> {items:[],message:string}; local only, no external fetch.

Policy: {sec_enabled:boolean,version:number,updated_at:string,next_run_at:string|null,scope:string}
Worker: {online:boolean,last_seen:string|null}
Job: {id,kind,title,status,trigger,progress_done:number,progress_total:number,checkpoint:object,attempts:number,error:string|null,created_at,updated_at,started_at:string|null,finished_at:string|null,requested_action:string|null,control_version:number,result:object|null,control_notice:string|null}
Statuses: QUEUED RUNNING SUCCEEDED PARTIAL FAILED PAUSE_REQUESTED PAUSED CANCEL_REQUESTED CANCELLED RETRY_WAIT.

No real analytics at P1: analysis routes show explicit foundation status, source capabilities, and offer bounded market probe. Never show invented analytic results. UI demo fixtures must be explicitly enabled by user and labelled throughout; they never enter live feed or analytics.

Testing limits apply only in development. Normal collection targets remain in config/collection-defaults.toml; they are P2/P3 implementation requirements, not implemented coverage. Cancel on a manual+automatic shared job withdraws manual demand and returns control_notice while automatic work continues. CANCELLED is terminal; retry supports FAILED/PARTIAL only.
