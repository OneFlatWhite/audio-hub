"""站点中心（v0.6 波次 2A）：六态计算与认证巡检。

- status：把 sites 表记录、运行时 config.sites 与 site_stats 合成页面用的六态视图，
  /sites 与总览共用一份口径。
- auth_check：run_auth_check_once —— 对所有有凭据的站点跑一遍 check_auth 并写回
  auth_status，由调度层（2B / 波次 3）决定何时调用，本包不注册 scheduler job。
"""
