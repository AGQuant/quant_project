# Scorr — Spec Registry Index

Generated 02-Sep-2026 from the live `session_log` table by `tools/gen_spec_index.py`
(681 doctrine entries across `architecture`, `canonical_spec`, `day_log`, `daylog`, `debug_learnings`, `decision`, `doctrine`, `framework`, `locked_spec`, `memory_rules`, `mobile_framework`, `propagation_map`, `protocol_one`, `ruling`, `spec`, `spec_locked`, `spec_registry`, `standing_rule`, `trading_learnings`, `week_log`).

**How to use this.** This is an INDEX, not the specs themselves. The full text of every
entry lives in the Railway DB and is the authority. To read one:

```sql
SELECT id, title, details FROM session_log WHERE id = <id>;
```

To find one by topic:

```sql
SELECT id, category, title FROM session_log
WHERE title ILIKE '%<keyword>%'
  AND category NOT IN ('archived_superseded')
ORDER BY id DESC;
```

`archived_superseded` is deliberately excluded — those are retired and must not be
treated as current. If a spec below says "supersedes id=X", X is archived.

**To regenerate:** `python3 tools/gen_spec_index.py`. Topical curation lives in
`tools/spec_index_sections.json` (id -> section). An entry with no mapping lands in the
dated recent section rather than being guessed into a topic.

---


## 1 · Operating rules & protocol — read these first

| id | Title |
|---|---|
| 15150 | PRODUCTION_MODE_STANDING_V1 — CC never holds a push (founder-locked 04-Aug-2026) |
| 13829 | ENGINE_LIVENESS_RULE_V1 (locked 02-Aug-2026, founder) |
| 13295 | MEMORY_SWEEP_FULL_SINGLE_SESSION_V2 (supersedes staged A-E plan) |
| 12625 | AUTO_FILE_GRANT_31JUL |
| 10093 | BACKLOG_STATUS_CHECK_V1 |
| 8850 | DETECT_REPAIR_RULE_V1 (locked 25-Jul-2026, founder) |
| 8807 | MAX_SUBSCRIPTION_ONLY_RULE_V1 (locked 25-Jul-2026, founder) |
| 8122 | NIGHT_BATCH_WINDOW_V1 - 00:00-06:00 IST protected + no-push (amends DEV_MODE_PUSH_RULE_V2 id=2667) |
| 7581 | CRITICAL_PATH_FREEZE_V1 |
| 6063 | CC_BATCH_QUEUE_MODEL_V1 |
| 5848 | MEMORY_HYGIENE_POLICY_V1 |
| 5700 | SCHEDULER_MASTER_RULE |
| 3041 | MAINTENANCE_LOCK_RULE_V1 |
| 2987 | CC_RULE_NAV_COMPLETE_SHIPPING_V1 |
| 2667 | DEV_MODE_PUSH_RULE_V2 |
| 2349 | DEPLOY_POLICY_ADDENDUM_09JUL |
| 2164 | CC_PUSH_SIGNAL_V1 |
| 2160 | DONE_WHAT_NEXT_PUSH_SIGNAL_V1 |
| 1713 | DEV_STAGE_DEPLOY_POLICY_V1 |
| 1586 | VERIFICATION_SCOPE_RULE_full_chain |
| 1473 | CC_MODEL_ROUTING_RULE_V1 |
| 1316 | CC_PUSH_RELAY_FALLBACK_PROTOCOL |
| 1306 | AUTO_CLEAN_SESSION_CHECK_RULE |
| 1175 | MEMORY_TAXONOMY_V1 |
| 967 | CC_TASK_PROTOCOL_V1 |
| 638 | CC_TASK_CLAIMING_PROTOCOL_V1 |
| 428 | CC_OPERATING_FRAMEWORK_V1 |
| 265 | SPEC_PROPAGATION_MAP |
| 239 | github_push_discipline |
| 235 | chat_rule_trigger_agenda_suggest |
| 196 | CANONICAL_ARCHITECTURE_v2_MaxCIO_vs_ClaudeAI |
| 156 | RAILWAY_MEMORY_RULES |
| 150 | CANONICAL_SPEC_REGISTRY |
| 101 | session_log_write_protocol_v2 |
| 100 | main_py_architecture_rule |


## 2 · V8 engine, baskets & paper execution

| id | Title |
|---|---|
| 15366 | SELL_MOMENTUM_V4_N5I — true weekly RSI REMOVED, mom_2d restored to spec (founder-locked 04-Aug-2026) |
| 7842 | BUY_REVERSAL_V6_FRESH_BOOK |
| 7828 | BUY_REVERSAL_V6_SPEC |
| 6055 | V8_INDEX_INTEL_UNIFIED_SPEC_V1_LOCKED |
| 5680 | BASKET_EDIT_AUTHORITY_RULE |
| 5650 | BUY_MOMENTUM_SPEC_V3_LOCKED_17JUL |
| 5647 | BUY_REVERSAL_SPEC_V5_SPRING_LOCKED_17JUL |
| 5646 | BUY_S1_BOUNCE_KILLED_17JUL |
| 5642 | SELL_OVERBOUGHT_KILLED_17JUL |
| 5626 | SELL_REVERSAL_SPEC_V6_R1FALL_LOCKED_17JUL |
| 4514 | SELL_MOMENTUM_SPEC_V4_N5I_LOCKED_16JUL |
| 3075 | FILTER_LOGIC_MASTER_INDEX_V1_12JUL2026 |
| 1919 | MOOD_GATE_INLINE_LAST_VALUE_DAYCHANGE_V1 |
| 1916 | PCR_TREND_MOOD_GATE_V1 |
| 1625 | FUNNEL_AUDIT_6baskets_06Jul |
| 1515 | V8_PARITY_HARNESS_SPEC_V1 |
| 1510 | V8_PRICING_PRINCIPLE_V1 |
| 1493 | V8_BACKTEST_OWNERSHIP_RULE |
| 1407 | V8_LIVE_METRICS_TRUTH_v2.4.0 |
| 1403 | V8_PAPER_EXECUTION_SPEC_V2 |
| 379 | SLOT_ARCHITECTURE_V2.4.0 |
| 244 | trade_check_v8_context_isolation |
| 226 | day_change_to_mom_2d_COMPLETE |
| 214 | V8_vs_TradeCheck_separation |
| 179 | v8_entry_rule_zone_gap50 |
| 175 | buy_reversal_gate_adaptive_qualification |
| 174 | v8_gvm_gate_relax_7_to_6 |
| 165 | adr_live_intraday_spec |


## 3 · V9 / V10 / V14 / V15 engines

| id | Title |
|---|---|
| 8169 | V9_SECTOR_PAIRS_SPEC_V3_BRAHMASTRA |
| 4734 | V15_MF_DATA_PIPELINE_SPEC_V1_FINAL |
| 4334 | V15_EQUITY_UNIVERSE_V1 |
| 3080 | V15_MF_BUILD_FRAMEWORK_NEXT_SESSION_REFERENCE |
| 3079 | V15_MF_INTELLIGENCE_ARCHITECTURE_V1 |
| 3069 | FABLE_V13_THEME_BRIDGE_STANDING_RULE |
| 3064 | V14_EXITS_AND_PP_FINAL_LOCKED |
| 3063 | V14_SETUPS_FINAL_V2_LOCKED |
| 3062 | V14_GATES_FINAL_V2_LOCKED |
| 3060 | V14_INTRADAY_ENGINE_SPEC_V1_LOCKED |
| 1662 | V11_ENDGAME_SEED_strategy_builder_platform |
| 374 | INTRADAY_PAPER_ENGINE_SPEC_V1 |


## 4 · Trade Check / Invest Check

| id | Title |
|---|---|
| 12289 | TC_SCANNER_V41_V8_ALIGNMENT_SPEC |
| 11047 | TC_OUTCOME_SIM_SPEC_V1 |
| 10237 | TC_VERSION_PROPAGATION_RULE_V1 |
| 9973 | TC_INTRADAY_TRUTH_V2 — Trade Check reads live intraday for everything except derivatives + frozen-by-design pillars |
| 9946 | TC_R1_MOOD_V2 — read V8 mood gate directly (amends v3.3.2 chain ids 143/209/263/264) |
| 9035 | TC_GATE_SIMPLIFICATION_V1 (founder-locked 26-Jul-2026) |
| 6632 | INVEST_CHECK_V3_0_GATE_CONVICTION_FINAL |
| 6624 | TC_R18_MOMENTUM_V1 |
| 6623 | TC_R20_DELTA_GVM_180D_V1 |
| 6622 | TC_R19_RELATIVE_STRENGTH_V1 |
| 6621 | TC_V4_R17_VALUATION_RULE_AND_EARNINGS_DEMOTE |
| 5682 | TC_V4_3_V8_ALIGNMENT_PRINCIPLE |
| 3019 | TC_V4_R16_FIB_RULE |
| 3010 | TC_V4_2_SELL_RULEBOOK_FINAL |
| 3005 | TRADE_CHECK_V4_FINAL_UNIFIED_SURFACE |
| 2926 | TRADE_CHECK_SPEC_V4_DUAL_STYLE |
| 959 | TC_CANONICAL_SPEC_V4 |
| 948 | TRADE_ANALYSIS_MANDATORY_3CHECK |
| 243 | trade_check_priority_order |


## 5 · Quant Baskets (QB) & model baskets

| id | Title |
|---|---|
| 6109 | QB_SIX_BASKET_PAGE_DESIGN_LOCKED |
| 6104 | CONTRA_VALUE_BASKET_V1_LOCKED |
| 6103 | BREAKOUT_52W_BASKET_V1_LOCKED |
| 6089 | QUANT_BASKET_FRAMEWORK_MASTER |
| 2970 | V12_QUANT_BASKET_BUILDER_MASTER_SPEC_V1 |
| 80 | basket_rebalancing_modus_operandi |


## 6 · Data, feed & scheduler

| id | Title |
|---|---|
| 15596 | CLOSING_PRICE_METHODOLOGY_BREAK_03AUG2026 — SEBI CAS: VWAP-based close becomes auction-based for Category I stocks (cc#855) |
| 14406 | GLOBAL_HEATSTRIP_V2 — two-level day/week heat strip above V8 global indices table (founder-locked 03-Aug 02:00) |
| 9116 | OPS_CONTROL_PLANE_V1_MASTERS |
| 6616 | SYSTEM_SPEED_CHECK_SPEC_V1 |
| 6339 | STOCK_OPTIONS_OFF_WS_ONDEMAND_CHAIN_V1 |
| 5422 | RAILWAY_PRO_UPGRADE_DISK_DOCTRINE_V2 |
| 1924 | RETENTION_POLICY_LOCKED_V2 |
| 1918 | PCR_INTRADAY_STALE_OI_GUARD_V1 |
| 1917 | GLOBAL_INDICES_PREVCLOSE_BUG_AND_247_REFRESH_V1 |
| 1781 | SCHEDULER_DIAGNOSIS_V2_full_coverage |
| 1770 | EARNINGS_CALENDAR_V2_ACCUMULATE |
| 1536 | INCIDENT_06JUL_feed_outage_0915_1005_root_cause |
| 1525 | FEED_PROVENANCE_CONSOLIDATION_intraday_equity |
| 1173 | STOCK_OPTIONS_CHAIN_SPEC_V1 |
| 1060 | DATA_SOURCE_RECONCILIATION_SPEC_30JUN2026 |
| 208 | SCHEMA_REGISTRY_SNAPSHOT |
| 167 | five_min_system_architecture |
| 166 | fyers_worker_ops |
| 79 | intraday_gap_fill_rule |


## 7 · Fundamentals, GVM & ops metrics

| id | Title |
|---|---|
| 13546 | SCRAPE_UNIVERSE_TOP500 (founder 02-Aug, supersedes id=10260 + season-pass) |
| 13517 | SEASON_PASS_SCRAPE_POLICY (founder 02-Aug) |
| 13348 | OPS_METRICS_INTERNAL_ONLY (founder-locked 01-Aug-2026) |
| 10260 | SCRAPE_UNIVERSE_HARD_BOUNDARY_V1 |
| 10056 | MQS WEIGHT CHANGE: Q50/R25/C15/S10 (was Q35/R30/C15/S20) |
| 9178 | SCRAPE_UNIVERSE_TOP500_NSE_V1 |
| 9108 | RESULT_SEASON_DATA_FRESHNESS_V1 |
| 9088 | MONTHLY_OPS_METRICS_CLEANUP_RITUAL_V1 |
| 7132 | OPS_METRICS_PEERSET_AND_WINDOW_LOCK_V1 |
| 7121 | SHAREHOLDING_SCRAPE_CADENCE_V1 |
| 7118 | SECTOR_KPI_LOCKED_FROM_DATA_V1 |
| 7117 | OPS_METRICS_PEER_BENCHMARK_SPEC_V1_1 |
| 5712 | SCREENER_UPLOAD_PROTOCOL_V1 |
| 5709 | OPS_METRICS_PHASE_SPLIT_V1 + STORAGE_GUARD_AMENDMENT_DOC_TEXTS |
| 5698 | OPS_METRICS_FRAMEWORK_V1 |
| 5685 | OPS_METRICS_SEGMENT_EXTENSIONS_V1_1 |
| 5684 | OPS_METRICS_SEGMENT_KPI_REGISTRY_V1 |
| 5678 | GVM_RATING_METHODOLOGY_UNIFICATION_V1 |
| 5634 | MC_SCRAPER_FRAMEWORK_CC500_17JUL |
| 269 | gvm_canonical_pillar_map |
| 85 | cap_category_definition |


## 8 · News, polish & editorial

| id | Title |
|---|---|
| 13616 | L2_POLISH_PROTOCOL_V1 (founder-locked 02-Aug — CC-on-command, no API, no card-filing) |
| 13456 | STOCK_VIEWS_FRAMEWORK_V2 (founder-locked 01-Aug-2026, supersedes id=10062 V1) |
| 13354 | POLISH_RESULTS_FRAMEWORK_V1 (founder-locked 01-Aug-2026) |
| 13353 | RESULT_ANALYSIS_V2_SAMPLE (founder-approved 01-Aug, reference for cc#784) |
| 10086 | POLISH_RESULT_UPDATE_FRAMEWORK_V1 |
| 8372 | STOCK_VIEW_CATEGORY_SPEC_V2_REVERSE_ENGINEERED |
| 8188 | POLISH_LANDING_RULE_V1 |
| 8157 | RESULT_CORNER_V2_DESIGN_R1_LOCKED |
| 7135 | RESULT_CORNER_SPEC_V1 |
| 6500 | VOLUME_V_BUTTON_UNIVERSAL_CARD_V1 |
| 6438 | RESULTS_R_BUTTON_UNIVERSAL_CARD_V1 |
| 4284 | NEWS_BRAND_SCRUB_RULE (LOCKED 16-Jul-2026) |
| 2492 | AI_EDITORIAL_STYLE_ADDENDUM_09JUL |
| 1923 | CONTENT_LEVEL_NEWS_DEDUP_V1 |
| 1660 | POSITION_NEWS_PIPELINE_V1 |
| 1293 | POLISH_BATCH_EDITORIAL_SPEC_V2 |
| 986 | KNOWLEDGE_HUB_POLISH_SPEC_V1 |
| 965 | daily_digest_format_v2 |
| 874 | POLISH_PRE_CHECK_RULE_V1 |
| 733 | POLISH_NEWS_SPEC_V1 |
| 445 | SCORR_INTELLIGENCE_POLISH_PROTOCOL_V3 |
| 413 | RESULT_ANALYSIS_FORMAT_V1 |
| 402 | NEWS_FETCH_SPEC_V1 |
| 110 | key_takeaway_update_rule |


## 9 · Portfolio, SmartGain & clients

| id | Title |
|---|---|
| 11983 | CLIENT_INDEX_CREDENTIALS_DECISION_V1 |
| 11510 | CLIENT_INDEX_V1_GOOGLE_SHEET_RETIREMENT |
| 6505 | SMARTGAIN_STALE_CARRIED_POSITIONS_MARK_CLOSED_20JUL |
| 6399 | PRICING_RULE_FUTURES_ONLY_LOCKED_20JUL |
| 4705 | SMARTGAIN_MHK40_CLEAN_RESET_16JUL |
| 4685 | SMARTGAIN_WEEK_PNL_DISPLAY_RULE_V1 |
| 2994 | PORTFOLIO_HEALTH_REPORT_SPEC_V1 |
| 2169 | ACCOUNT_TYPE_QTY_RULE_V1 |
| 2102 | CLIENT_POSITIONS_TRACKING_SPEC_V1 |
| 1393 | SMARTGAIN_DAILY_M2M_SPEC_V1 |
| 1170 | SMARTGAIN_ORDERBOOK_LINEAR_V1 |
| 1058 | INFINN_M2M_TRACKING_SPEC_V1 |
| 1049 | TRADE_DIRECTION_SOURCE_OF_TRUTH_V1 |
| 1008 | SMARTGAIN_WEEKLY_PNL_FRAMEWORK_V1 |
| 828 | SMARTGAIN_REVIEW_SPEC_V1 |
| 721 | TRADE_JOURNAL_FRAMEWORK_V1 |
| 566 | MULTI_BOOK_FUND_MANAGER_FRAMEWORK_V1 |
| 145 | journal_triggers_v1 |
| 143 | trade_review_framework_v3_final |


## 10 · Product, design & nav

| id | Title |
|---|---|
| 13551 | BASIC_POLISH_L1_CARD_DESIGN_V1 (founder-approved 02-Aug-2026) |
| 9016 | UI_PATTERN_MASTER_INDEX_V1 (locked 26-Jul-2026, founder) |
| 6372 | DERIVATIVE_COCKPIT_REDESIGN_V2 |
| 6133 | FIB_LEVELS_ZONE_COLOR_FIX |
| 2498 | PRICE_ACTION_PRIMACY_COCKPIT_ROLE |
| 2460 | SCORR_FULL_APP_REDESIGN_MAP_V1 |
| 637 | NAV_UX_SPEC_V1 |
| 432 | SCORR_PRODUCT_ARCHITECTURE_V2.1 |
| 376 | DEPLOY_REFLECTS_SCORR_IN_V1 |
| 375 | CANONICAL_NAV_SPEC_V1 |
| 222 | native_grammar_v1 |
| 219 | surface_names_locked |
| 146 | virtual_dashboard_format |
| 45 | trade_card_format |


## 11 · Max (AI CIO) & subscription

| id | Title |
|---|---|
| 13823 | MAX_FRAMEWORK_INDEX_V1 (master index, 02-Aug-2026 — single entry point for everything Max AICIO; read this first) |
| 13822 | MAX_IVR_TREE_V2 (comprehensive, locked 02-Aug-2026 — supersedes V1 id=13820; 3-stage build: L1 branches + L2 structured output THIS WEEK, L3 Gemini deferred to stage 3) |
| 13816 | MAX_IVR_ARCHITECTURE_V1 (locked 02-Aug-2026, founder + Fable Max) |
| 6615 | AI_INFRA_BILLING_ANTHROPIC_API_RETIRED_V1 |
| 6062 | AI_AUTH_ARCHITECTURE_V1_LOCKED |
| 852 | AI_CIO_MAX_POSITIONING_V1 |
| 851 | AI_CIO_MAX_FINAL_SPEC_V1 |
| 850 | AI_CIO_MAX_SESSION_BAR_V1 |
| 849 | AI_CIO_MAX_PRICING_V1 |
| 848 | AI_CIO_MAX_MODEL_SWITCH_V1 |
| 844 | AI_CIO_MAX_PLAN_FEATURES_V1 |
| 843 | SUBSCRIPTION_TIER_UPDATE_26JUN2026 |
| 221 | max_pricing_model_locked |
| 202 | SCORR_MAX_MODEL_CONFIG_v2 |
| 195 | Max CIO Memory Protocol v1 |


## 12 · Business, strategy & roadmap

| id | Title |
|---|---|
| 5846 | SCORR_ROADMAP_MASTER |
| 5845 | SCORR_STRATEGY_FRAMEWORK_MASTER |
| 2505 | NORTH_STAR_OCT2026_PROFIT_PRIMACY |
| 681 | GOLIVE_PREREQUISITES_V1 |
| 534 | SCORR_VISION_AND_ROADMAP_V1 |
| 528 | LINKEDIN_CONTENT_PLAN_V1 |
| 527 | SCORR_BUSINESS_PLAN_V1 |
| 434 | SCORR_PHASED_RELEASE_STRATEGY_V1 |
| 405 | SCORR_3NODE_OPERATING_SYSTEM_V1 |
| 341 | SCORR_PRODUCT_VISION_V1 |
| 286 | SCORR_2030_COMMITMENT |
| 280 | Scorr Launch Calendar 2026-2027 |
| 278 | Scorr Positioning Framework v1.0 |
| 233 | scorr_positioning_2year_roadmap_locked |


## 13 · Backtesting & learnings

| id | Title |
|---|---|
| 13286 | WEEK_5_TRADING_LEARNINGS (27-31 Jul 2026) — scale-out week, net +Rs 15,670 |
| 7990 | BACKTESTING_FRAMEWORK_V1 |
| 4711 | PIPELINE_LOCKED_17JUL_QUEUE_THEN_BACKTEST |
| 4708 | V7_BACKTEST_ARCHITECTURE_V1 |
| 1476 | TRADING_LEARNINGS_WEEK1_29JUN_03JUL |
| 743 | JULY_STABILISATION_AUDIT |
| 207 | DEBUG_LEARNINGS_CANONICAL |


## Not yet filed by topic — newest first

Entries added since the last curated pass. They are indexed and readable; they simply have no topical home yet. Add one to `tools/spec_index_sections.json` to file it.


### `architecture` — 2

| id | Title |
|---|---|
| 13786 | HTML_INJECTION_SUBSTRING_TRAP_V1 (cc#821) |
| 13587 | SHARED_COMPONENT_BEHAVIOUR_LOCK_V1 (cc#803) |


### `canonical_spec` — 20

| id | Title |
|---|---|
| 27289 | APPROVED_REF_EXACT_SPEC_RULE_V1 |
| 27283 | FABLE_RENDER_FIRST_RULE_V1 |
| 24183 | CHECK_FABLE_ROOM_TRIGGER_V1 — CC backup wake phrase (founder-set 17-Aug) |
| 3365 | V15_PAGE_DESIGN_LOCKED_R2 |
| 3364 | V15_DESIGN_REF_R2_LOCKED |
| 2533 | COWORK_PROTOCOL_V1 |
| 1651 | V8_EOD_NO_REQUALIFICATION_V1 |
| 1509 | FALLBACK_ARCHITECTURE_PRINCIPLE_V1 |
| 1068 | SCORR_RECURRING_TASKS_SPEC_V1 |
| 600 | TC_V36_LONG_RULES_UPDATE |
| 526 | BIJNA_MEETING_PREP_SPEC_V1 |
| 421 | SMART_GAIN_PORTFOLIO_SHEET |
| 407 | TRADE_STRUCTURE_ANALYZER_SPEC_V1 |
| 406 | PRICE_ALERT_SYSTEM_SPEC_V1 |
| 400 | INTRADAY_SCANNER_SELL_SPEC_V1 |
| 399 | INTRADAY_SCANNER_SPEC_V2 |
| 398 | SCORR_APP_LAYER3_DESIGN_SPEC |
| 397 | SCORR_APP_LAYER2_TECH_SPEC |
| 396 | SCORR_APP_LAYER1_PRODUCT_SPEC |
| 370 | trade_check_v3.5_graded |


### `debug_learnings` — 1

| id | Title |
|---|---|
| 30016 | V8_TIMESTAMP_TYPE_CHECK_V1 — naive-IST vs timestamptz columns confirmed 24-Aug |


### `decision` — 67

| id | Title |
|---|---|
| 33972 | VOLUME CANON: sign-off round on cc#1441 and cc#1446, six decisions made by Fable per founder instruction (make the call as per my intent) |
| 33844 | QSR_RETIREMENT -- founder order 30-Aug-2026: drop the QSR (Quality S1 Reclaim) strategy entirely, was unaware it existed, will rebuild later if needed |
| 27563 | FABLE_CTO_FORUM_AUTHORITY_V1 |
| 27555 | FOUNDER_RULINGS_20AUG_1050 |
| 27276 | DETAILED_SPEC_RULE_V1 |
| 27274 | UI_RENDER_CHECK_STANDING_RULE_V1 |
| 27250 | PRODUCTION_MODE_ON_20AUG_0640 |
| 24214 | SPRINT_2_TRIGGER convention + pending green comment (founder 17-Aug ~11:45) |
| 23256 | MONDAY_17AUG_VERIFICATION_CHECKLIST — standing instruction: on founder's "check cc progress" Fable runs this full verification, not just a queue read |
| 23240 | CC1049_RESCOPE_V1 — three fix classes retired; lever ladder: MALLOC_ARENA_MAX env -> probe series -> targeted subprocess cards |
| 23230 | CC1049_MEASUREMENT_COMPLETE_V1 — memory cost is runtime accumulation, not import residency |
| 23195 | BUY-SIDE EXPERIMENT (founder decision 16-Aug): buy_s1_pullback (spec 23186) takes the buy slots from Mon 18-Aug; buy_momentum V4 flips to record-only shadow (untouched otherwise). Checkpoint 15 trading sessions (~05-Sep). Revert = single flag: if S1 closed WR<55pct or V4 shadow clearly outperforms, V4 resumes trading. Founder framing: paper-book experiment backed by 1-year full-universe sim (115tr/61.7) + fresh-fortnight pass (6W/2L); fully aware sims are approximations; prepared to revert on failure. Build: cc#1051. |
| 22385 | BUY_MOMENTUM_V4 — 1-month live-parity simulation (Fable, 15-Aug): 15tr/53%/+24K (V3 actual) vs 7tr/71%/+1.03L (V4), caps 4.0 and 4.5 identical on live basis |
| 21595 | SECURELY_PROPOSAL_SENT_AND_PHASE1_RESEARCH_MANDATE_14AUG2026 |
| 21437 | SECURELY_COMMERCIALS_REVISED_V2_14AUG2026 |
| 20981 | SECURELY_13AUG_CALL_PREP_COMPLETE_PORTFOLIO_NOTES |
| 20979 | SECURELY_EKYA_IDENTIFIED_AGENT_KYC_13AUG2026 |
| 20607 | SECURELY_RELATIONSHIP_MAP_CORRECTION_13AUG2026 |
| 20606 | SECURELY_13AUG_CALL_ATTENDEES_CORRECTION |
| 20375 | Q1FY27_DOC_LABEL_DEFECT_AND_WEEKEND_RESCRAPE_LIST_12AUG2026 |
| 19746 | SECURELY_TRACK_MEETING_CONFIRMED_13AUG2026 |
| 19734 | HR_INVESTED_BASIS_RULE_V1 |
| 19503 | SECTOR_BENCHMARK_WIDE_BASIS_APPROVED_11AUG2026 |
| 19015 | SECURELY_ARTHAUM_CALL_SCHEDULED_13AUG2026 |
| 18767 | CC_TASK_NUMBERING_FIX_10AUG2026 |
| 18695 | PRODUCTION_MODE_WORKER_MARKET_HOURS_OVERRIDE_10AUG2026 |
| 18690 | PUSH_HOLD_10AUG2026 |
| 18686 | CC_ASSIGNMENT_STANDING_10AUG2026 |
| 18016 | MOBILE_POLISH_DOCTRINE_V1 — skeleton first, improve one comment at a time (founder 08-Aug) |
| 17977 | REMINDER_09AUG: unblock cc#898 + cc#768 |
| 17975 | LIVE_DATA_EVERYWHERE_V1 (founder 08-Aug) |
| 17964 | CC_TASK_SMALL_PIECES_V1 (founder 08-Aug) — split every comment into small cards, CC executes one by one |
| 17932 | CC_TASK_APPEND_ONLY_V1 (founder 08-Aug) — one comment = one NEW card, never amend a filed card |
| 17915 | HOME_BATCH_08AUG_CC896 — five founder fixes shipped in one push (Fable direct) |
| 17806 | MOBILE_BUILD_WORKFLOW_V2 (founder 08-Aug) — Fable builds first version of ALL screens; founder reviews everything at once; gap list goes to CC as ONE polish card with DO_NOT_REBUILD on the design system |
| 17783 | CHARTER_OVERRIDE_08AUG2026 — founder explicitly instructed Claude (Fable) to push cc#889–892 directly instead of CC |
| 17632 | SECURELY_FEE_STRUCTURE_LOCKED_07AUG2026 |
| 17027 | MOM_AUDIT_AND_NO_DISPUTE_DECISION_07AUG2026 |
| 17021 | CC872_SCOPE_CORRECTION_07AUG2026 — founder correction: the CURRENT website IS the professional dashboard; Raw Data + Matrix STAY on it; only Live News + Results tabs are removed |
| 17020 | CC872_RAWDATA_MATRIX_RESOLUTION_07AUG2026 — no conflict: tab removal proceeds in full; homelessness is temporary and founder-accepted |
| 17019 | OPS_METRICS_POLISH_SUPERSEDED_BY_RESULT_POLISH_07AUG2026 |
| 17013 | SECURELY_TRACK_UPDATE_PREP_AND_STRATEGY_07AUG2026 |
| 16924 | SECURELY_ENGAGEMENT_PROPOSAL_SENT_06AUG2026 |
| 16923 | BIJNA_NEELY_SECURELY_TRACK_V1 — advisory + independence plan (founder 06-Aug-2026) |
| 16916 | MOBILE_SCREEN_TRIAGE_V1 — Imp/May/Drop/Redesign tags per screen (Claude review 06-Aug-2026, per founder instruction: keep limited relevant screens, add later easily) |
| 16915 | APP_VS_WEB_AUDIENCE_SPLIT_V1 — app = retail, simple and intuitive; web = professional and institutional premium (founder-locked 06-Aug-2026) |
| 16911 | FOUNDER_DECISIONS_06AUG2026_PERF_AND_DESIGN |
| 16747 | FEED_OUTAGE_06AUG_FREEZE_AND_BACKFILL_RULE |
| 16710 | V21_KILLSWITCH_RETIRED_AND_CC873_RECONCILE_A_06AUG2026 |
| 16237 | TRADE_CARD_UNIFICATION_V1 — one trade card everywhere, no fill colour, wide stop-entry-CMP-target rail (founder 05-Aug-2026) |
| 16230 | PREVIEW_PROMOTION_DECISION_V1 — approved mobile screens are PROMOTED to real routes, not wired inside previews/ (founder 05-Aug-2026) |
| 16195 | PUSH_SIGNAL_EXTENDS_TO_CLAUDE_AI_V1 — every Claude AI push ends with DONE WHAT NEXT (founder 05-Aug-2026) |
| 15924 | TWO_PRODUCT_SPLIT_V1 — retail light vs institutional/HNI high-ticket (founder-set 05-Aug-2026) |
| 15899 | SMARTGAIN_FIFO_FROM_DB_V1 — founder instruction 05-Aug-2026, supersedes the platform-sync clause in 4685 |
| 15783 | SCORR_LAUNCH_ROADMAP_V2 — Jan-2027 launch, development timeline + public launch order (founder-set 05-Aug-2026, corrects FIRST_MONTH_LAUNCH_ROADMAP_V1) |
| 15671 | SIMPLE_LANGUAGE_BINDS_ALL_OUTPUT_V1 — founder 05-Aug-2026: LANGUAGE_RULE_02AUG (13354) extends to chat replies and every written surface |
| 9672 | TRACK_B_DB_CLEANUP_SAT_01AUG (proposed, console-only) |
| 9169 | RESULT_ANALYSIS_QUALITY_V2_PLACEHOLDER |
| 5428 | V15_MF_SAMPLE_MODEL_FRAMING |
| 3527 | FINKHOZ_STABLE_BASKET_REVIEW_AFFLE |
| 3523 | FINKHOZ_STABLE_BASKET_REVIEW_MOTHERSON |
| 3521 | FINKHOZ_STABLE_BASKET_REBALANCE_VBL |
| 1497 | BOARD_CLEARED_04JUL_FOUNDER_VISUAL_CONFIRM |
| 1495 | FOUNDER_DECISIONS_04JUL_FIVE_PARKED_ITEMS |
| 1479 | BRANDING_TAB_TITLES_DECISION_04JUL |
| 1399 | CC155_GATE_OVERRIDE_03JUL |
| 529 | TC v3.5 DIVISLAB Scale Decision — 25pt Accepted |


### `doctrine` — 12

| id | Title |
|---|---|
| 36394 | WOT_APPROVED_ONLY_V1 - founder 02-Sep-2026: Wall of Trades displays ONLY approved trades (trade_alerts status=approved, any origin: manual or engine signal approved from a surface). Unapproved engine output leaves the WoT display. Amends MANUAL_TRADE_ALERTS_V1 (34521) architecture_decision on what WoT SHOWS; the union code is hidden, not deleted. |
| 36200 | PCR_MOOD_BANDS_V2 - founder-locked 02-Sep-2026: PCR 1.00-1.50 = GREED; above 1.50 = CAUTIOUS if Nifty week return <= -1.0%, else EXTREME GREED. Lower bands unchanged. |
| 36144 | MODEL_EFFORT_CACHE_RULE_V1 — Fable 5.1 + Sonnet 5 by task fit, effort MEDIUM default, ultracode only for a named heavy card, preserve prompt-cache benefit. Founder-locked 02-Sep-2026. Amends PRODUCTION_MODE_V3 (29164) and MODEL_ARCHITECTURE_PHASE3_V1 (24-Aug daylog 30149). |
| 32076 | V10_OPT_LEG_IS_REPLICATION_NOT_HEDGE_founder_27aug |
| 29448 | TC_SCANNER_GATED_CONFIG_V1.1 — founder amendment 23-Aug: OBSERVATION MODE, no eligibility cuts (supersedes V1 thresholds/gates as filters) |
| 29447 | TC_SCANNER_GATED_CONFIG_V1 — locked 23-Aug-2026 (founder ruling after Fable simulation on cc#1211 data) |
| 29348 | HR_OPENING_BACKCALC_RULE_V1 — back-calculate opening value from broker yearly P&L when pre-ledger capital is unknown (locked 22-Aug-2026, first use Swati Kawale pid 187) |
| 29327 | SEGMENT_MIN_SIZE_RULE_V1.1 — corrected facts 22-Aug (supersedes 29040 scope 1) |
| 29164 | PRODUCTION_MODE_V3 — "production mode on" = CC re-arms ScheduleWakeup at 60 s for 2 hours, auto-claims in posted order, discusses only in the Fable Room (cc#1199 thread). FOUNDER: most important memory |
| 29016 | TASK_SURFACE_TAG_RULE_V1 — every cc_task carries a surface tag: APP, WEB, APP+WEB, or ENGINE |
| 29006 | RESULT_PEER_SOURCE_RULE_V1 — one profit source for R button peers, Result tab and app: fundamentals_history; top-3-by-GVM peer block becomes the shared component |
| 28971 | CC_QUEUE_DRAIN_RULE_V1 — claim one, push one, drain until empty; stale claims auto-release |


### `framework` — 17

| id | Title |
|---|---|
| 34521 | MANUAL_TRADE_ALERTS_V1 — founder 31-Aug: new manual price-alert + approval feature, replaces Intel in bottom nav, chat-style feed |
| 27782 | HKK09_PRO_INFINN_EXE_REPORT_PDF_B64_20AUG2026 |
| 27762 | HKK09_PRO_INFINN_EXE_REPORT_FORMAT_V1 |
| 25715 | FABLE_ROOM_MASTER_V1 — ONE ENTRY POINT for the Fable/CC operating system (founder-directed 18-Aug-2026) |
| 24632 | VISION_TO_SPRINT_LOOP_V1 — founder shows, Fable specs and runs, everything except trading (founder-locked 17-Aug-2026) |
| 24167 | FABLE_TEAM_V1 — protocol nodes, n8n-style, built incrementally (founder direction 17-Aug) |
| 24166 | PROTOCOL_ONE — daily platform health 1-pager (first run, 17-Aug 11:15 IST) |
| 23979 | APP_QA_AUDIT_V1 + APP_FLOW_CONSISTENCY_V1 — senior front-end audit of all app pages (source-truth, 17-Aug) |
| 22904 | Q1FY27_RESULT_POLISH_PLAN_15BATCH — founder 16-Aug-2026: the 154 unpolished are split into 15 batches, mcap-descending, run one at a time |
| 22388 | ENGINE_RESEARCH_STACK_V1 — founder 15-Aug-2026: ONE structure joining the iteration loop (22387), the backtesting framework (7990) and the learnings registries. Master index for all basket/engine research. |
| 22387 | ENGINE_ITERATION_LOOP_V1 — founder 15-Aug-2026: build on simulator, improve on live results |
| 21596 | PHASE1_ASSESSMENT_WORKING_DOCTRINE_V1 |
| 20980 | SECURELY_INDIA_STRATEGY_BRIEF_V1_13AUG2026 |
| 18037 | SCORR_RETAIL_THREE_LEVEL_LAUNCH_STRUCTURE_V1 |
| 17872 | MOBILE_DAY_LOG_08AUG2026 — 28 commits, all screens + depth + six-slot nav (extends 15914) |
| 17014 | SCORR_LAUNCH_APRIL2027_AND_MARKETING_MACHINE_SEED_07AUG2026 |
| 13820 | MAX_IVR_TREE_V1 (canonical editable node list — seeds max_ivr_tree; parent doc id=13816) |


### `locked_spec` — 2

| id | Title |
|---|---|
| 19621 | GVM_SECTOR_WIDE_BASIS |
| 19454 | GVM_G_YOY_GROWTH_V1 |


### `mobile_framework` — 7

| id | Title |
|---|---|
| 16222 | MOBILE_SESSION_COMPRESSION_05AUG2026 — full state of the mobile design session in one entry |
| 16202 | DISPLAY_PARITY_RULE_V1 — a marker added on web must appear on the app too (founder 05-Aug-2026) |
| 16185 | MOBILE_HOME_SINGLE_BOOK_V1 — Client and Test Trade dropped from mobile; SmartGain is My Portfolio (founder 05-Aug-2026) |
| 16170 | MOBILE_PREVIEW_PIPELINE_V1 — screen push order and status (05-Aug-2026) |
| 16157 | MOBILE_CARD_ROLE_RULE_V1 — nav card vs section card, and the slot-5 nav drift (05-Aug-2026) |
| 16065 | MOBILE_V8_SURFACE_MAP_R3 — tier + header value source for all eleven V8 tabs (design ref R3, 05-Aug-2026) |
| 15913 | MOBILE_APP_FRAMEWORK_V1 — canonical build framework for the Scorr mobile app (founder-set 05-Aug-2026) |


### `protocol_one` — 31

| id | Title |
|---|---|
| 36083 | PROTOCOL_ONE — platform health 1-pager (02-Sep-2026 AM, automated) |
| 35863 | PROTOCOL_ONE — platform health 1-pager (01-Sep-2026 PM, automated) |
| 35298 | PROTOCOL_ONE — platform health 1-pager (01-Sep-2026 AM, automated) |
| 35295 | PROTOCOL_ONE — platform health 1-pager (01-Sep-2026 AM, automated) |
| 34961 | PROTOCOL_ONE — platform health 1-pager (31-Aug-2026 PM, automated) |
| 34433 | PROTOCOL_ONE — platform health 1-pager (31-Aug-2026 AM, automated) |
| 33275 | PROTOCOL_ONE — platform health 1-pager (28-Aug-2026 PM, automated) |
| 32784 | PROTOCOL_ONE — platform health 1-pager (28-Aug-2026 AM, automated) |
| 32782 | PROTOCOL_ONE — platform health 1-pager (28-Aug-2026 AM, automated) |
| 32585 | PROTOCOL_ONE — platform health 1-pager (27-Aug-2026 PM, automated) |
| 32125 | PROTOCOL_ONE — platform health 1-pager (27-Aug-2026 AM, automated) |
| 32122 | PROTOCOL_ONE — platform health 1-pager (27-Aug-2026 AM, automated) |
| 31870 | PROTOCOL_ONE — platform health 1-pager (26-Aug-2026 PM, automated) |
| 31431 | PROTOCOL_ONE — platform health 1-pager (26-Aug-2026 AM, automated) |
| 31429 | PROTOCOL_ONE — platform health 1-pager (26-Aug-2026 AM, automated) |
| 31161 | PROTOCOL_ONE — platform health 1-pager (25-Aug-2026 PM, automated) |
| 30707 | PROTOCOL_ONE — platform health 1-pager (25-Aug-2026 AM, automated) |
| 30357 | PROTOCOL_ONE — platform health 1-pager (24-Aug-2026 PM, automated) |
| 29840 | PROTOCOL_ONE — platform health 1-pager (24-Aug-2026 AM, automated) |
| 29838 | PROTOCOL_ONE — platform health 1-pager (24-Aug-2026 AM, automated) |
| 28818 | PROTOCOL_ONE — platform health 1-pager (21-Aug-2026 PM, automated) |
| 28215 | PROTOCOL_ONE — platform health 1-pager (21-Aug-2026 AM, automated) |
| 28213 | PROTOCOL_ONE — platform health 1-pager (21-Aug-2026 AM, automated) |
| 27901 | PROTOCOL_ONE — platform health 1-pager (20-Aug-2026 PM, automated) |
| 27337 | PROTOCOL_ONE — platform health 1-pager (20-Aug-2026 AM, automated) |
| 27035 | PROTOCOL_ONE — platform health 1-pager (19-Aug-2026 PM, automated) |
| 26429 | PROTOCOL_ONE — platform health 1-pager (19-Aug-2026 AM, automated) |
| 26427 | PROTOCOL_ONE — platform health 1-pager (19-Aug-2026 AM, automated) |
| 25678 | PROTOCOL_ONE — platform health 1-pager (18-Aug-2026 PM, automated) |
| 25198 | PROTOCOL_ONE — platform health 1-pager (18-Aug-2026 AM, automated) |
| 24585 | PROTOCOL_ONE — platform health 1-pager (17-Aug-2026 PM, automated) |


### `ruling` — 55

| id | Title |
|---|---|
| 34700 | FINKHOZ_SCREENERS_IMPORT_V2 — LIQUIDCASE resolved: drop, no substitute — Helios + Defence cleared |
| 34696 | FINKHOZ_SCREENERS_IMPORT_V1 — founder 31-Aug: 7 Finkhoz curated baskets added to the public Screeners tab strip, 5 of 7 cleared for import now |
| 34683 | SCREENERS_TABLE_SORT_V1 — founder 31-Aug: clickable column-header sorting on the Screeners page |
| 34470 | INDEX_INTEL_SYMBOL_FONT_BUMP_V1 — founder 31-Aug: symbol column font +2 notches across all Index Intel tables, column widths stay fixed |
| 34463 | CHART_CARD_GVM_PILLAR_TOGGLE_V1 — founder 31-Aug: price chart GVM overlay gets a small toggle to switch between GVM/Growth/Value/Momentum lines, default GVM |
| 34417 | CHART_CARD_FUTURES_MARKER_V1 — founder 31-Aug: mark futures-tradable symbols with ^ everywhere the shared Chart (C) card shows a bare ticker |
| 34393 | INDEX_INTEL_HIDE_GATECHIP_V1 — founder 31-Aug: hide the GATE · BEARISH · 3F chip in the page header, not required |
| 34378 | HOME_POS_DEFAULT_TAB_BY_PNL_V1 — founder 30-Aug: Index Positions card defaults to whichever of NIFTY/BANKNIFTY is more profitable (or less lossy) on initial load, not always NIFTY |
| 34239 | CHART_CARD_ZOOM_BACKFILL_V1 — founder 30-Aug: panning/zooming toward the edge of loaded history should auto-fetch older candles rather than hit blank space |
| 34237 | CHART_CARD_PIVOT_LABEL_POSITION_V1 — founder 30-Aug: pivot values move from the top-left strip to sit above their own lines, reusing the fib-tag positioning pattern |
| 34228 | CHART_CARD_GRID_RESTORE_V1 — founder 30-Aug: bring back light grey horizontal gridlines on the shared C/Chart card, reversing cc#988 |
| 34222 | HR_REPORT_RISK_METRICS_V1 — founder 30-Aug: new Portfolio Risk Metrics card above Sector Split — Beta, Std Dev, Sharpe, Sortino, Max Drawdown, R-squared |
| 34221 | HR_REPORT_HOLDINGS_TABLE_DROP_FROM_ATH_V1 — founder 30-Aug: remove FROM ATH column from the Holdings — Full Detail table, display only |
| 34220 | HR_REPORT_FOOTER_TRIPTYCH_REFLOW_V1 — founder 30-Aug: Result Analysis widens to span columns 1+2, Key Highlights moves into column 3 above Expert Take |
| 34219 | HR_REPORT_SECTOR_SPLIT_TOP15_V1 — founder 30-Aug: Sector Split bar goes top 10 -> top 15, bar height increased for visual weight |
| 34218 | HR_REPORT_RATING_CONTRIBUTORS_BREAKUP_V1 — founder 30-Aug: RATING PARAMETERS bars stay, add Contributors (company-wise) and Breakup (parameter-wise heatmap, 21 params x holdings) tabs beneath |
| 34212 | V10_LOGS_INFO_BUTTON_V1 — founder 30-Aug: (i) button in the View Logs overlay shows exact live spec + backtest result per index, read from /api/v10/summary, never hardcoded |
| 34201 | HOME_WRITING_CARD_RESTORE_FUTURES_BLOCK_V1 — founder reverses 30-Aug: FUTURES range bar comes back on Index Open Writing Positions card, alongside OPTIONS; supersedes the removal half of 34185 |
| 34185 | HOME_INDEX_OPEN_WRITING_POSITIONS_CARD_V1 — founder 30-Aug-2026 from live phone render: rename, drop FUT block + index toggle, both indices stacked, Bullish/Bearish tag, VIEW LOGS opens options leg with switch hidden |
| 34184 | INDEX_INTEL_CLIENT_FACE_IS_OPT_LEG_V1 — founder 30-Aug-2026: client-facing Index Intel shows the OPTION WRITING leg as the headline; futures leg runs in background, not shown to clients |
| 29672 | RESULT_DOT_RULE_V2 (founder-approved 23-Aug ~22:4x — supersedes 29519 peer basis + adds season lifecycle) |
| 29663 | RESULT_ROW_DOTS_ONLY_RULE_V1 (founder 23-Aug ~22:0x) |
| 29631 | NEWS_TAG_VOCAB_RULE_V1 (founder 23-Aug 21:25) |
| 29630 | PIVOT_LADDER_ORDER_RULE_V1 (founder 23-Aug 21:19) |
| 29610 | RESULT_VERDICT_SOURCE_RULE_V1 (founder 23-Aug ~20:00) |
| 29528 | INTEL_VOLATILITY_FLOW_TRIM_V1 (founder 23-Aug ~16:58) |
| 29519 | RESULT_DOT_RULE_V1 (founder 23-Aug ~17:1x IST) |
| 29510 | INTRADAY_CHART_SESSION_JOIN_RULE_V1 (founder 23-Aug 16:19) |
| 29505 | DAILY_ONLY_SERIES_1D_RULE_V1 (founder screenshot 23-Aug 16:12) |
| 29504 | TC_SCANNER_INTRADAY_ENGINE_SWITCH_V1 (founder 23-Aug, option B) |
| 29490 | TC_SCANNER_SURFACE_TRIM_V1 (founder 23-Aug ~14:52 IST) |
| 29158 | WEB_THEME_TOKEN_LOCK_V1.1 — contract grows: tint scale, second surface, one win/loss vocabulary; all 37 web files migrate; sprint becomes 15 pushes |
| 29138 | TC_SCORE_100_V1 — TC score shown out of 100, not 10; denominator = registry weight sum per bucket; bands rescaled x10 |
| 29123 | SELL_REVERSAL_V7C — fall-from-R1 gate retired; R1-touch stays; 11 gates strict-AND |
| 29088 | LIVE_NEWS_FIXED_HEIGHT_V1 — Home LIVE NEWS card never changes height: fixed row height, headline clamped to 2 lines, pager pinned |
| 29067 | V10_TICK_WINDOW_V1 — V8 writer AND Index Intel (V10) both take their LAST tick at 15:20 IST; nothing writes after it |
| 29040 | SEGMENT_MIN_SIZE_RULE_V1 — no GVM segment below 3 stocks; two 2-stock segments merged 22-Aug |
| 29030 | TC_LOCK_VERIFY_22AUG — registry vs locks: zero weight drift; 27974 label correction (R7 is MONTHLY RSI); two CC readings confirmed |
| 28373 | MOOD_CARD_CHIPS_BEVEL_V1 + FOOTER_CHART_DEFECT — founder 21-Aug ~10:35 from live render: check chips become bevel-curve buttons (no slant cut); NIFTY footer chart renders no data on any window; expanded chart has no way back |
| 28367 | PCR_CARD_DIGEST_CTA_V1_1 — founder clarification 21-Aug ~10:34: not just the button — increase vertical spacing of ALL card items top to bottom so the face fills its height |
| 28362 | PCR_CARD_DIGEST_CTA_V1 — founder 21-Aug ~10:31 from live render: Daily Digest link becomes a rich full-width CTA using the empty bottom space of the MARKET MOOD PCR card |
| 28349 | HOME_INDEX_INTEL_SINGLE_BEVEL_V1 — founder 21-Aug ~10:28 from live phone renders: mood-card edge button OUT, index strip footer note OUT, one bevel button IN, index rows deep-link to V10 trades view |
| 28179 | MOOD_WORD_GLIMPSE_ONLY_V1 — founder 21-Aug ~09:10 IST from live phone render: per-letter blink REMOVED, glimpse effect only, cycle 10s -> 4s |
| 28171 | ANTHROPIC_KEY_DEAD_SECTOR_BRIEFS_PARKED — founder 21-Aug: no top-up; sector briefs move to a future CC sprint |
| 27980 | QSR_ENGINE_V1 — Quality S1 Reclaim daily equity engine (founder-locked 20-Aug-2026 ~23:50 IST; fills the Wall equity slot dropped tonight; name QSR provisional, founder may rename) |
| 27979 | INVESTMENT_CHECK_V2_LOCKED (founder-locked 20-Aug-2026 ~23:05 IST; supersedes investment_check v1.0 spec id 324 on engine go-live; v1 stays serving until V2 ships + founder validates) |
| 27977 | TC_SELLMOM_WEIGHTS_LOCKED_V2 + TC_ALL_FOUR_LOCKED (founder-locked 20-Aug-2026 ~22:20 IST; completes the four-bucket lock set 27974/27975/27976) + DISPLAY RULING: rules ordered by weight, heaviest first |
| 27976 | TC_SELLREV_WEIGHTS_LOCKED_V2 (founder-locked 20-Aug-2026 ~22:00 IST; sweep-selected, supersedes Fable V1 sell-rev seeds; forward-validation-bound) |
| 27975 | TC_BUYMOM_WEIGHTS_LOCKED_V1 (founder-locked 20-Aug-2026 ~21:25 IST; part of TC_SCORE_V1 27957; includes TWO condition-level amendments to the locked BUY-MOM rules) |
| 27974 | TC_BUYREV_WEIGHTS_LOCKED_V1 + TC_CALIBRATION_TARGETS_V1 (founder-locked 20-Aug-2026 ~21:05 IST; part of TC_SCORE_V1 27957 / cc#1172) |
| 27951 | V14_ENGINE_SILENCED (founder ruling 20-Aug-2026 ~18:40 IST: "not our type") — engine inactive, research archived, V14-B parked post-launch |
| 27944 | SURFACE_TAG_V1 + CC_PROGRESS_REPORT_FORMAT_V2 (founder-set 20-Aug-2026 ~17:45 IST; extends CC_PROGRESS_REPORT_FORMAT_V1 22310) |
| 27943 | FOUNDER_DIRECT_TO_CC_WEB_V1 (founder-set 20-Aug-2026 ~17:20 IST; addendum to ROLE_CHARTER_V4 27934) |
| 27934 | EXECUTION_MODEL_PHASE_3 — ROLE_CHARTER_V4 (founder-set 20-Aug-2026 ~16:25 IST; supersedes ROLE_CHARTER_V3 17868 and refines PUSH_MODES_V2 27933) |
| 27933 | PUSH_MODES_V2 — FABLE_APP_DIRECT (founder-approved 20-Aug ~16:20 IST, supersedes PUSH_MODES_V1 mode definitions; ROLE_CHARTER_V3 unchanged) |


### `spec` — 32

| id | Title |
|---|---|
| 36294 | PCR_READ_INTERPRET_V1 - PCR card (i) popover: plain-words read of the current PCR band plus a next-day evidence line from pcr_daily x raw_prices; too-few line until 20 scored sessions per band. Founder ask 02-Sep-2026. Extends PCR_MOOD_BANDS_V2 (36200) and pcr_mood.py (cc#1568). |
| 36283 | OI_STRUCTURE_INTERPRET_V1 - Max Pain card (i) popover: scenario read of max pain + call wall + put wall vs spot, each interpretation carrying its own evidence from a daily structure snapshot; labelled framework-only until >= 20 scored sessions per scenario. Founder ask 02-Sep-2026. |
| 35016 | TC_SCANNER_R1_V1 |
| 35003 | TRADE_CONTROL_V1 |
| 34998 | INDEX_INTEL_WEB_R1_V1 |
| 34993 | DAILY_DIGEST_R4_V1 |
| 33993 | VOLUME_METRICS_CANON \u2014 FINAL LOCK: every volume metric in Scorr is served by exactly 4 -- RVOL, VOL P, Delivery Ratio, Accumulation. No exceptions. |
| 33988 | VOLUME CANON: reference naming locked (Vol R/P/D/AD) + per-surface migration plan for the 7 active vol_ratio consumers |
| 33849 | VOLUME_METRICS_CANON_V3 -- Accumulation consolidation: 3 independent implementations found, Deriv Cockpit 21d version chosen as canonical |
| 33843 | VOLUME_METRICS_CANON_V2 -- founder-locked 30-Aug: vol_ratio fully retired, RVOL+VOL P replace it everywhere (both display and gates), canon is now 4 real formulas |
| 33833 | VOLUME_METRICS_CANON_V1.1 -- founder execution order 30-Aug: VOL P replaces VOL-x as the DISPLAY form of metric 2 |
| 33832 | VOLUME_METRICS_CANON_V1 -- founder-locked 30-Aug-2026: 5 surviving volume metrics, full touchpoint mapping, VOL P naming |
| 30129 | INVESTMENT_SCANNER_UNIVERSE_V1.1 (founder-amended 24-Aug-2026 — adds leg 3, supersedes 30091 universe rule) |
| 30091 | INVESTMENT_SCANNER_UNIVERSE_V1 (founder-locked 24-Aug-2026) |
| 29970 | CLIENT_BASKET_REBALANCE_MULTIPLIER_V1 |
| 29274 | HR_XIRR_EVERYWHERE_V1 |
| 29197 | HR_LEDGER_PAYIN_PAYOUT_V1 |
| 29136 | TC_SCORE_ENTRY_REPLAY_V1 — 5-day replay: entry by TC score out of 10 (not filter pass), all four buckets, every 15 min in market hours, exit +-2% with square-off; threshold x hold-days sweep |
| 29075 | WEB_THEME_TOKEN_LOCK_V1 — one web token contract, every page consumes it, theme choice persists site-wide (web twin of THEME_TOKEN_LOCK_V1 28379) |
| 29015 | RESULT_CORNER_SEGMENT_POPOUT_V1 — R button shows segment name, clickable; popout table of every reporter in the segment: result date, Sales YoY, PAT YoY, beat/miss, FY27 est, C·A·R·D buttons |
| 29002 | DIGEST_RESULTS_CARD_V2.2 — result sheet ends with SEGMENT PEERS button: top 10 same-segment reporters by mcap, table Symbol / Result date / YoY Sales / YoY PAT |
| 28995 | DIGEST_RESULTS_CARD_V2.1 — swipe deck of 4, continuous loop over full analysed universe, Large/Mid/Small filter |
| 28986 | DIGEST_RESULTS_CARD_V2 — Yesterday's Results card sourced from result_analysis_v2, news-style bottom sheet, View Detail → Result Corner |
| 28379 | THEME_TOKEN_LOCK_V1 — founder-approved sprint 21-Aug ~10:40: scan + lock full GOLD NIGHT design schema as tokens, build AQUA WHITE parallel set, template-switch mechanism; founder reviews full app in evening session, finalises, then both themes ship for Home |
| 27957 | TC_SCORE_V1 — one normalized 0-10 score per bucket, Fable-instinct weights (founder-directed 20-Aug ~19:30 IST; founder chart-review next day; weight sweet-spot backtest after 3 trading days + 1-yr horizon) |
| 24255 | PRODUCTION_MODE_AUTOPOLL_V1 — "production mode on" arms a 5-minute Fable Room poll for 2 hours (founder-set 17-Aug-2026) |
| 23903 | APP_SECTION_DISTRIBUTION_V1 — Daily Digest vs Index Intel (founder-approved 08:45 17-Aug) + design refs R1 pushed |
| 23897 | HEADING_COLOR_RULE_V1 (founder-locked 17-Aug 08:27) — addendum to THEME_TOKENS_R51_V1 (23878) |
| 23878 | THEME_TOKENS_R51_V1 — canonical Scorr colour system (founder-locked direction, 17-Aug-2026 morning) |
| 23877 | NEW_TASK_PER_COMMENT_V1 (founder-locked 17-Aug 06:2x) |
| 23865 | PUSH_MODES_V1 — founder-locked operating modes (17-Aug-2026, 06:0x IST) |
| 23359 | APP_UPDATE_FRAMEWORK_V1 — how app-wide visual/product updates ship (addendum to MOBILE_APP_FRAMEWORK_V1 15913; locked from 16-Aug TELEMETRY DROP + Fight Card rollout) |


### `spec_locked` — 144

| id | Title |
|---|---|
| 34206 | V10_BANKNIFTY_SPEC_V3_10M_250_LOCKED — founder-locked 30-Aug-2026 (evening): ST 10m / ATR150 / mult 4.0 + EMA 3/10 on 30m gate, SL250 / T250; supersedes 34192 (20m/4.0/5-13/150-150) before its first live tick |
| 34192 | V10_BANKNIFTY_SPEC_V2_20M_LOCKED — founder-locked 30-Aug-2026: ST 20m / ATR150 / mult 4.0 + EMA 5/13 on 30m gate, SL150 / T150; supersedes BNF ST150/3.0 10m + EMA3/10, SL150/T300 |
| 34174 | V10_NIFTY_GEOMETRY_V2_SL100_T150 — founder-locked 30-Aug-2026 at LEVEL 2 (5m-stepped replay); signal ST150/3.0 + EMA3/10 UNCHANGED after L1+L2 sweeps; supersedes NIFTY SL100/T200 |
| 33626 | NEWS_POLISH_BATCH_SIZE_V3 -- 10 per batch: 2 AI Editorial + 5 Domestic + 2 Global + 1 IPO (founder-locked 29-Aug-2026) |
| 33587 | LIVE_FEED_STALENESS_PATTERN_V1 — >1hr fallback rule is a shared platform pattern, PCR is the first consumer |
| 33583 | PCR_TICK_AND_EOD_DISPLAY_V2 — EOD/fallback citation returns, gated on >1hr live-feed absence, always stamped |
| 33582 | PCR_TICK_AND_EOD_DISPLAY_V1 — 15:20 universal cutoff (supersedes 33581's brief 15:25 lock); EOD write stays, EOD display removed |
| 33581 | PCR_INTRADAY_TICK_WINDOW_V1 — last tick locked at 15:25 IST, founder-locked 29-Aug-2026 (distinct from V10_TICK_WINDOW_V1's 15:20) |
| 33568 | V10_5M_FLOOR_DECISION_V1 — pan-back floor stays at 1-Jul-2026, founder-locked 29-Aug-2026 |
| 33334 | PCR_MOOD_MAPPING_V2 — Cautious/Neutral relabel + speedometer gauge display, founder-locked 28-Aug-2026 (supersedes 18024 wording only) |
| 30420 | WALL_OF_TRADES_GROUPS_V2 — supersedes cc#1000/cc#1175 four-group lock (founder 24-Aug) |
| 30263 | COMMIT_LOG_TIMESTAMP_V1 |
| 30147 | INVESTMENT_SCANNER_SCORE_AND_RULES_V1 (founder-locked 24-Aug-2026; universe = 30129 V1.1; validated live this session) |
| 27321 | V8_TIMING_RULES_V1 |
| 26386 | STRATEGY_PHASE_MODEL_V1 — Phase 1 establish the mix, Phase 2 let it run (founder-set 19-Aug-2026) |
| 26363 | SELL_REVERSAL_V7B_CANDIDATE — founder-directed 19-Aug-2026, SHADOW/RECORD-ONLY (supersedes the V7 variant in this row) |
| 25730 | OPM_EXPANSION_BFSI_RULING_V1 — OPM Expansion IS scored for BFSI (founder-ruled 18-Aug-2026) |
| 25714 | CC_TASK_ID_RULE_V1_1 — placeholder is born status=draft, never pending (amends 22358; founder-directed 18-Aug-2026) |
| 25708 | CLIENT_HEALTH_REPORT_DESIGN_FRAMEWORK_V3 |
| 24566 | SWALLOWED_EXCEPTION_RULE_V1 — ok must mean the body did its work (locked 17-Aug-2026) |
| 24490 | APP_MOTION_MODEL_V1 — one axis one job; vertical sections, horizontal decks, tap for depth (founder-locked 17-Aug-2026) |
| 24477 | DESIGN_REF_IN_FORUM_V1 — every new design/build HTML is appended to cc_task_logs (founder-locked 17-Aug-2026) |
| 24444 | THEME_STAR_RULE_V1 — green/red star + pass-count sort on the V8 theme table (founder 17-Aug) |
| 24402 | SPRINT_PUSH_SIZING_V1 — every sprint splits into 5-15 pushes; single mode is rare (founder-locked 17-Aug-2026) |
| 24220 | VIX_COLOR_RULE_V1 — VIX chip semantics (founder 17-Aug 11:55, from live GOLD NIGHT screenshot) |
| 24161 | COFOUNDER_ACCOUNTABILITY_V1 — the split (founder-locked 17-Aug ~11:15) |
| 24138 | CC_COMMS_LOOP_V1 — the table is the meeting room (founder 17-Aug ~11:00) |
| 24081 | APP_MODUS_OPERANDI_V1 — app framework amendment (founder 17-Aug ~10:30) |
| 24051 | HOURLY_GATE_ROLLING_V1 — V5 hourly gate is a rolling 60m (12x5m session bars) change (founder 10:10 17-Aug) |
| 23991 | REPORT_DRIVEN_EXECUTION_V1 — Fable takes the driving seat on app/product (founder-locked 17-Aug 09:40) |
| 23980 | BLACK_GOLD_WHITE_THEME_SPEC_V1_DRAFT — Phase 2 skin (founder-commissioned 17-Aug; NOT for current build) |
| 23260 | PUSH_WHENEVER_POSSIBLE_V1 — staging production mode: no task held for verification-scheduling comfort |
| 23247 | INDEX_SYMBOL_CONVENTION_V1 |
| 23245 | BUG_FIRST_RULE_V1 — bug found midway blocks the main task until fixed; data quality outranks speed |
| 23197 | BUY_MOMENTUM_V5 (founder amendment 16-Aug): basket name STAYS buy_momentum; rule set upgrades in place to the 23186 spec (S1-touch + PP 0-1.5 + day<=2 + 10:15-13:00, no twr gate, no mom gate). V4 gate-set runs as record-only shadow (bm_v4_shadow). Fresh-book rule per 7842: V5 stats = V5-stamped trades only. Checkpoint ~05-Sep; revert = one flag. Supersedes the separate-basket naming in 23186/23195; all other content of 23186 stands as the V5 rule source. |
| 23186 | BUY_S1_PULLBACK CANDIDATE V1 — founder-locked 16-Aug-2026 as PAPER CANDIDATE basket (full-universe 5-min sim 115tr/61.7%/H1 61.3/H2 62.3; fresh-fortnight OOS 6W/2L) |
| 22406 | SEGMENT_TAXONOMY_V2 — founder-locked 15-Aug-2026: exactly TWO segment groupings are KEPT (22 futures themes + 130 GVM segments). Everything else is a ghost. Corrects and replaces 22405. |
| 22405 | SEGMENT_TAXONOMY_V1 — founder-locked 15-Aug-2026: exactly TWO segment groupings exist. No third taxonomy, no ghost segments. Retires the 22-theme layer. |
| 22386 | BUY_MOMENTUM_SPEC_V4_FINAL — founder-locked 15-Aug-2026: ONE hard gate added to V3 — mom_2d in [0, 4.0]. Sector_week gate DROPPED. Amends 22375; V3 (5650) base otherwise unchanged. |
| 22375 | BUY_MOMENTUM_SPEC_V4 — founder-locked 15-Aug-2026: two hard gates added to V3 (mom_2d hard band [0,4] + sector_week > 0). Entry-time cutoff REJECTED by founder. Supersedes 5650 (V3) on these gates only. |
| 22358 | CC_TASK_ID_RULE_V1 — founder-approved 15-Aug-2026: the cc# in every task title IS the cc_tasks row id |
| 22353 | TC_BEST_OF_FOUR_V1 — founder 15-Aug-2026: trade check scores all four style cards; BEST = highest score/max PERCENTAGE, displayed alone with view-detail |
| 22344 | TABLE_SPEC_LINEAGE_15AUG — authoritative pointer: 22342 (+base 22321) is the ONLY live basket/master table spec; 22301, 22318, 22324, 22338 are ALL superseded |
| 22342 | V8_BASKET_MIRROR_SPEC_V2.1 — founder 15-Aug (third statement of the same requirement): mirror means IDENTICAL RENDERING, not identical column names |
| 22338 | V8_TABLE_VIEWS_V2.1 — founder 15-Aug-2026: basket tabs render the EXACT master mirror (Status + master operational columns); signal metrics leave the basket table |
| 22324 | V8_TABLE_VIEWS_V2 — founder 15-Aug-2026: one registry for DEFINITIONS, per-surface VIEWS for display. Supersedes 22301 (V8_UNIFIED_TABLE_SPEC_V1). |
| 22321 | V8_BASKET_MIRROR_SPEC_V2 — founder 15-Aug-2026: baskets MIRROR the master table; MASTER NEVER CHANGES. Supersedes V8_UNIFIED_TABLE_SPEC_V1 (22301) |
| 22318 | V8_TABLE_MIRROR_V2 — founder 15-Aug-2026: MASTER table reverts to pre-cc#1023 form; basket tabs MIRROR it one-way. Supersedes V8_UNIFIED_TABLE_SPEC_V1 (22301). |
| 22314 | FUNNEL_TRUTH_V1 |
| 22310 | CC_PROGRESS_REPORT_FORMAT_V1 — founder-locked 15-Aug-2026: how Fable presents CC queue/progress |
| 22306 | HEARTBEAT_IST_CONTRACT_V1 |
| 22301 | V8_UNIFIED_TABLE_SPEC_V1 — founder 15-Aug-2026: ONE column spec for the master open-positions table and all 4 basket tables |
| 22296 | MARKER_GLYPH_V5 — founder 15-Aug-2026: activity marker is a LIGHTNING BOLT, green star/circle both retired |
| 22294 | MARKER_GLYPH_V4 — founder 15-Aug-2026: GREEN activity marker is a filled star on BOTH sides; circle glyph fully retired platform-wide |
| 22292 | LADDER_VIEW_V2 |
| 21769 | FUT_BOOK_CUTOVER_V1 addendum — Futures tab purity: zero equity-priced values rendered |
| 21766 | FUT_BOOK_CUTOVER_V1 — founder 14-Aug-2026: fresh futures-priced realised book, cutover 14-Aug close |
| 21764 | MARKER_GLYPH_V3 — founder 14-Aug-2026: TC marker STRONG-only filled amber star both sides; pivot SELL = red STAR not circle |
| 21516 | SECURELY_BOARD_PROPOSAL_V1_FINAL_14AUG2026 |
| 18687 | POLISH_NO_SELF_REFERENCE_RULE_10AUG2026 |
| 18205 | RESULT_OUTPUT_TERM_DEFINITION_V1 |
| 18078 | TC_SELL_CARDS_MIN_V1 — founder-locked 08-Aug-2026: minimal SELL-MOM (12 rules) + SELL-REV (11 rules) |
| 18064 | TC_BUYMOM_TRIM_V1 — founder-locked 08-Aug-2026: drop R15 + R8, add Delivery confirm (BUY-MOMENTUM card only) |
| 18062 | TC_BUYREV_TRIM_V1 — founder-locked 08-Aug-2026: R18 relaxed, R15 dropped, R17 tightened (BUY-REVERSAL card only) |
| 18053 | GREEN_STAR_ACTIVITY_V1 — founder-locked 08-Aug-2026: volume/OI activity marker on open positions |
| 18052 | PIVOT_STAR_V2 — founder-locked 08-Aug-2026: open-positions scope, new conditions, star for buy / circle for sell |
| 18024 | PCR_MOOD_MAPPING_V1 — contrarian bands, founder-locked 08-Aug-2026 |
| 17782 | MOBILE_REBUILD_IN_PLACE_V1 — founder 08-Aug-2026: NO rollback to old app; rebuild every mobile screen in place to approved-preview quality; app carries the old app's retail breadth (adds Screeners, Sector, Holdings, FPC) |
| 17271 | RESULT_NEWS_ALWAYS_POLISH_V1 — founder rule 07-Aug-2026 |
| 17252 | NEWS_POLISH_BENCHMARK_FABLE_V1 — founder-set quality benchmark (07-Aug-2026) |
| 17173 | POLISH_LANDING_RULE_V1_1 — TIMESTAMP BASE IS NAIVE IST (amends 8188, incident 07-Aug) |
| 15851 | PIVOT_STAR_V1 — blue star at S1 on BUY signals, red star at R1 on SELL signals (founder-locked 05-Aug-2026, cc#856) |
| 15784 | BOARDROOM_ESCALATION_RULE_V1 |
| 13549 | OPS_METRICS_SEGMENT_KPI_HEADERS_ARCHIVE (frozen 02-Aug-2026, for future vendor integration) |
| 10079 | POLISH_OPS_METRICS_FRAMEWORK_V1 |
| 9073 | CC648_COMPLETION_GATE_AND_4Q_BACKFILL_V1 |
| 8858 | HEALTH_REPORT_CLIENT_FORMAT_V1.1 - Result section = compact table (founder amendment 25-Jul) |
| 8857 | HEALTH_REPORT_CLIENT_FORMAT_V1 (LOCKED 25-Jul-2026, founder) |
| 7130 | ENGINE_WATCHDOG_STANDING_RULE |
| 7129 | ENGINE_WATCHDOG_V1 |
| 6640 | TC_V4_CEILING_FINAL_V1 LIVE (cc#586) |
| 6625 | TC_V4_CEILING_FINAL_V1 |
| 6098 | MID_CAP_V2_LOCKED |
| 6097 | LARGE_CAP_V2_LOCKED |
| 6094 | SMALL_CAP_V2_LOCKED |
| 6086 | ALPHA_MULTICAP_V2_FINAL_DGVM_LOCKED |
| 6085 | ALPHA_MULTICAP_SPEC_V2_FINAL_LOCKED |
| 6084 | ALPHA_MULTICAP_SPEC_V2_LOCKED |
| 5415 | V15_MF_SCRAPE_DEACTIVATION_17JUL |
| 5093 | SCORR_BASKET_FUTUREGIANTS_V1 |
| 5084 | SCORR_BASKET_MARKETLEADERS_V1 |
| 5081 | SCORR_BASKET_HIDDENVALUE_V1 |
| 5065 | SCORR_BASKET_MULTIBAGGER_V1 |
| 5019 | SCORR_BASKET_DIVYIELD_V1 |
| 2172 | LINKEDIN_POST_FORMAT_V1 |
| 2066 | SMARTGAIN_ORDERBOOK_LINEAR_V1_ADDENDUM_2 |
| 2057 | SMARTGAIN_WEEKLY_PNL_FRAMEWORK_V1_ADDENDUM_1 |
| 1922 | POSITION_NEWS_COVERAGE_GAP_FIX_AND_POLISH_QUOTA_V1 |
| 1920 | POSITION_NEWS_MOVE_TO_V8_V1 |
| 1652 | V8_EOD_NO_REQUALIFICATION_V1_ADDENDUM_1 |
| 1498 | MONDAY_06JUL_CHECKLIST_TRIGGER |
| 1272 | SMARTGAIN_ORDERBOOK_LINEAR_V1_ADDENDUM_1 |
| 1155 | R6_VOLUME_TIME_ADJUSTED_SPEC_V1 |
| 737 | LINKEDIN_POST_SPEC_V1 |
| 673 | TC_DISPLAY_FIX_SPEC_V1 |
| 636 | NEWS_DISPLAY_SPEC_V1 |
| 635 | NEWS_POLISH_FRAMEWORK_V1 |
| 634 | NEWS_POLISH_SPEC_V1 |
| 371 | nifty50_screener_BUILD_SPEC |
| 368 | trade_check_v3.4_unified |
| 367 | invest_check_v2.0 |
| 366 | trade_check_interpretation_layer_v1 |
| 337 | buy_reversal_dynamic_nifty_filter_v1 |
| 336 | buy_reversal_filter_optimisation_v1 |
| 324 | investment_check_v1 |
| 305 | SECTOR_OPS_METRICS_COMPANY_LIST_v1 |
| 304 | SECTOR_OPS_METRICS_SPEC_v1 |
| 264 | trade_check_v3.3.2_R6R8_merge_delta |
| 263 | trade_check_v3.3.1_R13_ATR_delta |
| 245 | prepare_report_context |
| 241 | trade_check_v341_partial_pass |
| 238 | trade_check_v34_weighted_engine |
| 236 | V4_STRATEGY_AND_DUAL_DASHBOARD |
| 209 | trade_check_output_format |
| 197 | strategy_analysis_layer |
| 180 | ANTHROPIC_API_KEY_DEPLOYED_TO_RAILWAY |
| 162 | scorr_marketing_infographic_template |
| 136 | filter_config_reorder |
| 135 | gs_refresh_all_timeout_fix |
| 134 | raw_data_tab_update |
| 133 | filter_config_range3d_update |
| 132 | filter_config_sector_update |
| 125 | qb_rebalance_schedule |
| 124 | qb_exit_rules |
| 90 | input_raw_content_structure_v2 |
| 87 | input_raw_refresh_schedule_v3 |
| 84 | v2.9.4_build_complete |
| 83 | overview_takeaway_content_rules |
| 38 | Add NIFTY/BANKNIFTY 1-min to Fyers feed |
| 35 | Fyers feed: post-close ghost-row fix (Day 38) |
| 31 | Day 38 - Entrypoint unified to main:app + live engine + full audit |
| 15 | Secondary Tagline Locked |
| 14 | Phase 2 — Broker Integration & 1-Tap Execution LOCKED |
| 9 | Two-stack interconnected journal system |


### `spec_registry` — 2

| id | Title |
|---|---|
| 29791 | R_BUTTON_FULL_UNIVERSE_V1 — R card serves the FULL GVM universe at full spec minus the Detailed Analysis section (founder-locked 24-Aug-2026) |
| 29790 | RESULT_DOT_RULE_V3 — dot basis moves to STANDALONE OWN-ESTIMATES (founder-locked 24-Aug-2026, supersedes V2 29672 and the cc#1238 V1 median compute) |


### `standing_rule` — 3

| id | Title |
|---|---|
| 18337 | V8_PNL_CANON_V1 — one book formula, retired baskets excluded everywhere |
| 18278 | FABLE_DIAGNOSIS_FIRST_V1 |
| 18213 | OPS_METRICS_RETIRED |


### `trading_learnings` — 8

| id | Title |
|---|---|
| 23046 | BACKTESTING DOCTRINE V1 (founder-directed, 16-Aug-2026) — standing rules for every backtest, sweep, or simulation, in-chat or harness |
| 23043 | UNIVERSE COVERAGE RULE (founder escalation 16-Aug): the v8_metrics Jun25-Jun26 backfill covered only 93 of 209 futures symbols — every historical backtest since (Fable in-chat sims AND the planned cc#1047 replay) was silently a ~45pct-universe test. Founder caught it via a trade-count contradiction. Root fixed by cc#1048 (v8_metrics full-universe backfill; GVM source rows already existed in gvm_history). STANDING RULE: every backtest/replay result states its evaluable universe count in the TITLE line; if evaluable < live universe, that is the first sentence of the report, not a caveat. cc#1047 batch blocked on cc#1048 completion. |
| 23026 | BUY_PULLBACK candidate FAILED first out-of-sample test (16-Aug): last 13 live sessions (30-Jul to 14-Aug, fyers_eq, full 209 universe) = 16 closed, 6W/10L, 37.5pct WR, -0.75 avg — vs 67.6pct in the year sim. Candidate demoted from ready-to-propose to doubtful. Live V4 same window: 3W/3L, net -49.5K (window hostile to buy-side generally). |
| 23013 | BUY_PULLBACK candidate found (Fable 5-min sweep, 16-Aug): mom_2d_live [-2,0) + day_1d_live [0.3,2] over V3 quality gates = 29tr / 75.9pct WR / +1.65 avg, stable 76.5/75.0 across year-halves. Mirror-image sibling of buy_momentum (no overlap with mom [0,4]). Founder bar 65pct met in-sim; NOT locked — Layer 2 routing, real-basis scoring required via cc#1047 recording before any proposal. |
| 23002 | BUY_MOMENTUM 5-min approximation backtest (Fable in-chat, 16-Aug): CONTRADICTS the live retro and EOD sim — blocked-by-mom cohort 61.9% WR / +0.72 avg across ALL quarters on a 35-symbol subset. Conclusion: approximations cannot settle the mom-gate question; entry-timing sensitivity is extreme; real-writer replay (cc#1047) is the only arbiter. |
| 22952 | BUY_MOMENTUM filter sweep on 12-month approximation book (16-Aug): day_1d upper cap is the only variant improving BOTH year-halves; monotonic 1.5-3.0; stacking with mom [0,4] underperforms day_1d alone |
| 22945 | BUY_MOMENTUM V3-vs-V4 12-month EOD-approximation backtest (Fable in-chat, 16-Aug) — mom gate is REGIME-DEPENDENT; blocked cohort +55.6% WR over full year, but gate clearly helps in current Q2-Q3-2026 regime |
| 21726 | FEED_INCIDENT_14AUG2026_EXTENDED_UNIVERSE_SILENT_PARTIAL_DEATH |


## Day and week logs — dated records, NOT specs

Indexed so an id can be looked up, kept apart so none of them is ever read as a rule. The DB carries both `day_log` and `daylog` spellings.

| id | Title |
|---|---|
| 34970 | DAY LOG 31-Aug-2026 (Mon) -- 22 cc tasks closed, 40 news rows polished, F&O OI drift fixed, V8 miss ledger revived after ~3 months dead, FINKHOZ weekly content format locked |
| 34169 | DAY LOG 30-Aug-2026 (extended session): Volume Metrics Canon closed end-to-end, QSR retired, ~20-task Index Intel UI overhaul, 3 real engine bugs found and fixed |
| 33834 | DAY LOG 30-Aug-2026 (daytime) -- Claude web session: VOLUME_METRICS_CANON locked + sprints filed, 6 FINZ factsheets refreshed, cc#1432-1439, Finkhoz doc registry |
| 33808 | DAY LOG 29-30 Aug 2026 (overnight into 30-Aug) — Claude web session: cc#1419-1431 resolved (13 tasks, all done), volume-ratio landscape audit, news-polish backlog check |
| 33611 | DAY LOG 29-Aug-2026 — Mobile Home redesign marathon (cc#1374-1418), resume checklist for next session |
| 31162 | DAY LOG 24-25 Aug 2026 (overnight into 25-Aug) — Claude web session: 28 cc_tasks resolved (1304-1331), 2 news polish batches, FINKHOZ teaser deliverable |
| 30424 | DAY LOG 24-Aug-2026 (Mon evening) — Claude web session: SmartGain reconciled, news polish batch, 12 UI/engine cards drained, Wall of Trades group-lock superseded, one card carried to next session |
| 30149 | DAY LOG 24-Aug-2026 (Sunday session, Monday market) — Investment Scanner designed+locked+carded, 3 frontend cards, model architecture principle, health checks |
| 29902 | DAY LOG 24-Aug-2026 (Mon) — morning session with Fable: polish batch 4503-4532 live, RESULT_DOT_RULE_V3 + R_BUTTON_FULL_UNIVERSE_V1 locked, GitHub token restored, all gates released, board drained to CC |
| 29674 | DAY LOG 23-AUG-2026 (Sunday) — production marathon: ~45 cards, Intel rebuilt, result experience redefined |
| 29449 | DAY LOG 23-Aug-2026 (Sunday) — clients, TC scanner ruling, CC queue state |
| 29324 | DAY LOG 22-Aug-2026 — 20+ shas, CC drain loop unblocked (60s wake), three "registered is not live" bugs killed, V7-C locked, TC score to /100, theme lock sprints opened |
| 27935 | DAY LOG 20-Aug-2026 — 30 tasks closed, Index Intel redesigned 7/7, two systemic calc bugs killed, Phase 3 execution model born |
| 27837 | DAY LOG 20-Aug-2026 (PM, Fable) — HKKR09 Pro actual-performance report built from broker ledger; SmartGain reconciled; format + PDF archived |
| 27836 | DAY LOG 20-AUG-2026 — polish run 30, V8 signal-row sprint done (unverified), Index Intel redesign sprint written |
| 27835 | DAY_LOG_20AUG2026 |
| 26691 | DAY LOG 19-Aug-2026 — V7-B live, sector source moved to futures theme, WS cut 920→430, feed clean all session |
| 26364 | DAY LOG 18/19-Aug-2026 — Fable session: OPM Expansion class bug, Sprint 4 rulings, sell_reversal V7-B, audio gap closed |
| 24565 | FABLE DAY LOG 17-AUG-2026 — session close, pickup point for next session |
| 24118 | QA_TESTING_OBSERVATIONS_17AUG — sandbox notes for pickup in a future chat (deliberately NOT appended to R4: sha 528a7e1 is live in cc#1077) |
| 23379 | DAY LOG 16-Aug ADDENDUM (23:00-23:50) — late-night founder comment loop on home + GVM |
| 23358 | Week_10Aug_16Aug_2026 |
| 23356 | DAY LOG 16-Aug-2026 (Sunday, evening half) — TELEMETRY DROP theme live app-wide, Home scoreboard shipped, GVM Fight Card built+wired+data-fixed same night |
| 23234 | DAY LOG 16-Aug-2026 (Sunday) — V5 live, 1049 measured, queue reset |
| 22283 | 14-Aug-2026 — Fable session: cc#1017 feed fix verified-in-part, MARKER_GLYPH_V3, FUT_BOOK_CUTOVER_V1 shipped + re-base DB-verified, SEC D% stale-comment cleanup |
| 19745 | DAY_LOG_11AUG2026 |
| 19043 | 10-Aug-2026 — CC session: cc#645/646/647 (feature branch), cc#994-1000 shipped to main (9 tasks) |
| 18510 | DAY LOG 09-Aug-2026 — full platform quality sprint: 44 cards, P&L canon, mobile app hardening |
| 18212 | DAY LOG 08-09 Aug 2026 — mobile polish sprint cc#900-937, TC card reform, marker family, Intel blind spot |
| 17620 | DAY_LOG_07AUG2026_EOD — sign-off addendum to 17163 |
| 17163 | DAY_LOG_07AUG2026 |
| 16909 | DAY_LOG_06AUG2026 |
| 15914 | DAY_LOG_05AUG2026 |
| 15495 | Day_05_04Aug2026 |
| 15079 | Day_04_03Aug2026 |
| 13560 | Week_27Jul_02Aug_2026 |
| 13559 | Day_03_02Aug2026 |
| 13302 | ROLLUP_INDEX_JUL2026_week_log |
| 13301 | ROLLUP_INDEX_JUL2026_task |
| 13300 | ROLLUP_INDEX_JUL2026_day_log |
| 13299 | ROLLUP_INDEX_JUN2026_week_log |
| 13298 | ROLLUP_INDEX_JUN2026_task |
| 13297 | ROLLUP_INDEX_MAY2026_task |
| 13282 | Day_02_31Jul_01Aug2026 |
| 5847 | ROLLUP_INDEX_JUN2026_TO_04JUL |


## Supersession trail — what is NO LONGER current

These are named here because an index that simply drops a retired entry leaves a reader
unable to tell "retired" from "never existed", and a rule that reads as absent is a rule
someone re-adopts by accident.

| retired id | was | now live |
|---|---|---|
| 17868 | ROLE_CHARTER_V3 — Fable owns all app tasks, CC benched | **27934** ROLE_CHARTER_V4 / EXECUTION_MODEL_PHASE_3 |
| 22301, 22318, 22324, 22338 | V8 table specs, four rounds | **22342** (+ base 22321), per the 22344 lineage pointer |
| 22405 | SEGMENT_TAXONOMY_V1 | **22406** SEGMENT_TAXONOMY_V2 |
| 5650 gates | BUY_MOMENTUM V3 gate set | **23197** BUY_MOMENTUM_V5 (rule source 23186) |
| 324 | investment_check v1.0 | **27979** INVESTMENT_CHECK_V2 — *on V2 go-live; v1 still serving* |
| 22310 | CC_PROGRESS_REPORT_FORMAT_V1 | **27944** REPORT_FORMAT_V2 (extends, does not retire) |
