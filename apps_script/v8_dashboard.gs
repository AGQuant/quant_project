/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║   SCORR V8 — Quant Long-Short Tracker                            ║
 * ║   Front-end for Google Sheet "V8 Final CLS V"                    ║
 * ║   Tabs: Master_Dashboard, Buy_Reversal, Buy_Momentum,           ║
 * ║   Sell_Reversal, Sell_Momentum, Sell_Overbought, In_Position,   ║
 * ║   Trade_Log, Raw_Data, Filter_Scan                              ║
 * ║   Data source: Railway V8 + PAPER engine endpoints              ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

const SCRIPT_VERSION = '1.7.2';
const SCRIPT_RAW_URL = 'https://raw.githubusercontent.com/AGQuant/quant_project/main/apps_script/v8_dashboard.gs';
const BASE_URL = 'https://quantproject-production.up.railway.app';
// FULL_CONTENT_PLACEHOLDER_DO_NOT_USE