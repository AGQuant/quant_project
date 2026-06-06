/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║   SCORR V8 — Core: Config, Menu, API, Refresh Entry Points      ║
 * ║   Part 1 of 2  →  v8_dashboard_core.gs                          ║
 * ║   Companion  →  v8_dashboard_tabs.gs                            ║
 * ║                                                                  ║
 * ║   Changelog v2.1.1:                                              ║
 * ║     - ncol cap 11→13 (shows all filters incl sector_week/month)  ║
 * ║   Changelog v2.1.0:                                              ║
 * ║     - refreshAll timeout fix (sell funnels last)                 ║
 * ║     - refreshSellFunnels dedicated menu item                     ║
 * ║     - Raw Data 21 cols + sector_week/month + pivots              ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

const SCRIPT_VERSION      = '2.1.1';
const SCRIPT_RAW_URL_CORE = 'https://raw.githubusercontent.com/AGQuant/quant_project/main/apps_script/v8_dashboard_core.gs';
const SCRIPT_RAW_URL_TABS = 'https://raw.githubusercontent.com/AGQuant/quant_project/main/apps_script/v8_dashboard_tabs.gs';

const BASE_URL = 'https://quantproject-production.up.railway.app';

const SHEETS = {
  DASH:    'Master_Dashboard',
  BR:      'Buy_Reversal',
  BM:      'Buy_Momentum',
  SR:      'Sell_Reversal',
  SM:      'Sell_Momentum',
  SO:      'Sell_Overbought',
  POS:     'In_Position',
  LOG:     'Trade_Log',
  RAW:     'Raw_Data',
  SCAN:    'Filter_Scan',
};

const BASKETS = ['buy_reversal', 'buy_momentum', 'sell_reversal', 'sell_momentum', 'sell_overbought'];

const COLORS = {
  DARK_HEADER:    '#1F2937',
  SUBHEADER:      '#374151',
  ALT_ROW:        '#F9FAFB',
  CARD_BG:        '#FFFFFF',
  BUY_REV:        '#2563EB',
  BUY_MOM:        '#1D4ED8',
  SELL_REV:       '#EA580C',
  SELL_MOM:       '#C2410C',
  SELL_OB:        '#9333EA',
  PASS_BG:        '#DCFCE7',
  PASS_TEXT:      '#15803D',
  FAIL_BG:        '#FEE2E2',
  FAIL_TEXT:      '#B91C1C',
  NEUTRAL_BG:     '#F3F4F6',
  NEUTRAL_TEXT:   '#4B5563',
  PROFIT:         '#16A34A',
  LOSS:           '#DC2626',
  FLAT:           '#6B7280',
  WHITE:          '#FFFFFF',
  MUTED_LIGHT:    '#D1D5DB',
  BORDER_STRONG:  '#111827',
  BORDER_SOFT:    '#E5E7EB',
  AVAIL_BG:       '#166534',
  FILLED_BG:      '#7C3AED',
};

const FONTS = {
  TITLE:    { family: 'Inter',       size: 14, weight: 'bold' },
  SUBTITLE: { family: 'Inter',       size: 11, weight: 'bold' },
  HEADER:   { family: 'Inter',       size: 10, weight: 'bold' },
  BODY:     { family: 'Inter',       size: 10, weight: 'normal' },
  MONO:     { family: 'Roboto Mono', size: 10, weight: 'normal' },
  BIG_NUM:  { family: 'Inter',       size: 18, weight: 'bold' },
};

const BASKET_META = {
  buy_reversal:    { label: 'Buy Reversal',    color: COLORS.BUY_REV,  emoji: '▲' },
  buy_momentum:    { label: 'Buy Momentum',    color: COLORS.BUY_MOM,  emoji: '▲' },
  sell_reversal:   { label: 'Sell Reversal',   color: COLORS.SELL_REV, emoji: '▼' },
  sell_momentum:   { label: 'Sell Momentum',   color: COLORS.SELL_MOM, emoji: '▼' },
  sell_overbought: { label: 'Sell Overbought', color: COLORS.SELL_OB,  emoji: '⚠' },
};

// Max filter columns in funnel waterfall.
// sell_momentum now has 12 filters + sector = 12 total.
// Set to 13 to always show all filters including sector_week/month.
const FUNNEL_MAX_COLS = 13;


// ════════════════════════════════════════════════════════════════════
//   MENU + TRIGGERS
// ════════════════════════════════════════════════════════════════════

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('🟣 Scorr V8')
    .addItem('🔄 Refresh All',              'refreshAll')
    .addSeparator()
    .addItem('📊 Refresh Dashboard',        'refreshMasterDashboard')
    .addItem('🔺 Refresh Buy Baskets',      'refreshBuyBaskets')
    .addItem('🔻 Refresh Sell Baskets',     'refreshSellBaskets')
    .addItem('🔻 Refresh Sell Funnels',     'refreshSellFunnels')
    .addItem('⚠️  Refresh Sell Overbought', 'refreshSellOverbought')
    .addItem('📍 Refresh In Position',      'refreshInPosition')
    .addItem('📒 Refresh Trade Log',        'refreshTradeLog')
    .addItem('📋 Refresh Raw Data',         'refreshRawData')
    .addItem('🔍 Refresh Filter Scan',      'refreshFilterScan')
    .addSeparator()
    .addItem('🆕 Check for Updates',        'pullLatestFromGitHub')
    .addItem('🏗️  Build All Tabs',          'buildAllTabs')
    .addItem('⏰ Setup Auto-Refresh (5 min)', 'setupTriggers')
    .addItem('🛑 Stop Auto-Refresh',         'stopTriggers')
    .addItem('ℹ️  About / Version',          'showVersion')
    .addToUi();
}

function setupTriggers() {
  stopTriggers();
  ScriptApp.newTrigger('scheduledRefresh').timeBased().everyMinutes(5).create();
  SpreadsheetApp.getActiveSpreadsheet().toast('Auto-refresh enabled — every 5 min', 'Scorr V8', 4);
}

function stopTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'scheduledRefresh') ScriptApp.deleteTrigger(t);
  });
}

function scheduledRefresh() {
  const ist     = new Date(Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd HH:mm:ss'));
  const day     = ist.getDay();
  const minutes = ist.getHours() * 60 + ist.getMinutes();
  if (day === 0 || day === 6) return;
  if (minutes < 555 || minutes > 930) return;
  refreshAll();
}


// ════════════════════════════════════════════════════════════════════
//   UPDATE CHECKER
// ════════════════════════════════════════════════════════════════════

function pullLatestFromGitHub() {
  let remoteVersion = 'unknown';
  try {
    const response = UrlFetchApp.fetch(SCRIPT_RAW_URL_CORE, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      SpreadsheetApp.getUi().alert('❌ GitHub fetch failed: HTTP ' + response.getResponseCode());
      return;
    }
    const match = response.getContentText().match(/const\s+SCRIPT_VERSION\s*=\s*['"]([^'"]+)['"]/);
    remoteVersion = match ? match[1] : 'unknown';
  } catch (e) {
    SpreadsheetApp.getUi().alert('❌ Could not reach GitHub:\n' + e);
    return;
  }

  if (remoteVersion === SCRIPT_VERSION) {
    SpreadsheetApp.getUi().alert('✅ Up to date',
      `You're on the latest version.\n\nCurrent: ${SCRIPT_VERSION}\nRemote: ${remoteVersion}`,
      SpreadsheetApp.getUi().ButtonSet.OK);
    return;
  }

  const html = HtmlService.createHtmlOutput(
    `<!DOCTYPE html><html><head><style>
      body{font-family:-apple-system,sans-serif;padding:20px;color:#1F2937;}
      h2{margin-top:0;color:#2563EB;} .row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #E5E7EB;}
      .label{color:#6B7280;} .val{font-family:'Roboto Mono',monospace;font-weight:bold;} .new{color:#16A34A;}
      .url-box{background:#F3F4F6;padding:10px;border-radius:6px;font-family:monospace;font-size:11px;word-break:break-all;margin:8px 0;user-select:all;}
      button{background:#2563EB;color:white;border:none;padding:10px 20px;border-radius:6px;font-size:13px;font-weight:bold;cursor:pointer;width:100%;margin-top:6px;}
      .steps{background:#FEF3C7;padding:12px;border-radius:6px;margin-top:12px;}
      ol{padding-left:18px;font-size:12px;line-height:1.7;color:#374151;}
    </style></head><body>
      <h2>🆕 New version available</h2>
      <div class="row"><span class="label">Installed</span><span class="val">${SCRIPT_VERSION}</span></div>
      <div class="row"><span class="label">Latest</span><span class="val new">${remoteVersion}</span></div>
      <p style="margin:14px 0 4px"><strong>Core script:</strong></p>
      <div class="url-box" id="u1">${SCRIPT_RAW_URL_CORE}</div>
      <button onclick="copy('u1','b1')" id="b1">📋 Copy Core URL</button>
      <p style="margin:12px 0 4px"><strong>Tabs script:</strong></p>
      <div class="url-box" id="u2">${SCRIPT_RAW_URL_TABS}</div>
      <button onclick="copy('u2','b2')" id="b2">📋 Copy Tabs URL</button>
      <div class="steps"><strong>To install:</strong><ol>
        <li>Apps Script editor → open <code>v8_dashboard_core.gs</code> → paste Core</li>
        <li>Open <code>v8_dashboard_tabs.gs</code> → paste Tabs</li>
        <li>Save (Ctrl+S) → reload sheet</li>
      </ol></div>
      <script>
        function copy(id,btn){
          const text=document.getElementById(id).innerText;
          navigator.clipboard.writeText(text).then(
            ()=>{document.getElementById(btn).innerText='✓ Copied';},
            ()=>{const r=document.createRange();r.selectNode(document.getElementById(id));
              window.getSelection().removeAllRanges();window.getSelection().addRange(r);
              document.execCommand('copy');document.getElementById(btn).innerText='✓ Copied (fallback)';}
          );
        }
      </script>
    </body></html>`
  ).setWidth(500).setHeight(500);
  SpreadsheetApp.getUi().showModalDialog(html, 'Scorr V8 Update Available');
}

function showVersion() {
  const html = HtmlService.createHtmlOutput(
    `<!DOCTYPE html><html><head><style>
      body{font-family:-apple-system,sans-serif;padding:20px;color:#1F2937;}
      h2{color:#9333EA;margin-top:0;} .row{padding:6px 0;}
      .label{color:#6B7280;display:inline-block;width:130px;}
      .val{font-family:'Roboto Mono',monospace;font-weight:bold;}
    </style></head><body>
      <h2>🟣 Scorr V8</h2>
      <div class="row"><span class="label">Version</span><span class="val">${SCRIPT_VERSION}</span></div>
      <div class="row"><span class="label">API base</span><span class="val">${BASE_URL}</span></div>
      <div class="row"><span class="label">Tabs</span><span class="val">${Object.keys(SHEETS).length}</span></div>
      <div class="row"><span class="label">Baskets</span><span class="val">${BASKETS.length}</span></div>
      <div class="row"><span class="label">Funnel max cols</span><span class="val">${FUNNEL_MAX_COLS}</span></div>
      <div class="row"><span class="label">2D gate</span><span class="val">day_change (2-day net c2c%)</span></div>
      <p style="margin-top:16px;color:#6B7280;font-size:12px;">Split: core.gs + tabs.gs · All calc in DB</p>
    </body></html>`
  ).setWidth(400).setHeight(300);
  SpreadsheetApp.getUi().showModalDialog(html, 'About');
}


// ════════════════════════════════════════════════════════════════════
//   API CALLS
// ════════════════════════════════════════════════════════════════════

function fetchJSON(endpoint) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const response = UrlFetchApp.fetch(BASE_URL + endpoint, {
        muteHttpExceptions: true,
        headers: { 'Accept': 'application/json' },
      });
      const code = response.getResponseCode();
      if (code === 200) return JSON.parse(response.getContentText());
      Logger.log(`API ${endpoint} returned ${code} (attempt ${attempt})`);
    } catch (e) {
      Logger.log(`fetchJSON ${endpoint} failed (attempt ${attempt}): ${e}`);
    }
    if (attempt === 1) Utilities.sleep(600);
  }
  return null;
}

var _RAW_CACHE = null;
function getRawMetricsCached() {
  if (_RAW_CACHE === null) _RAW_CACHE = fetchRawMetrics();
  return _RAW_CACHE;
}
function clearRawCache() { _RAW_CACHE = null; }

function fetchMarketMood()     { return fetchJSON('/api/v8/market_mood'); }
function fetchFilterConfig(b)  { return fetchJSON('/api/v8/filter_config/' + b); }
function fetchQualified(b)     { return fetchJSON('/api/v8/qualified/' + b + '?limit=200'); }
function fetchFunnelCounts(b)  { return fetchJSON('/api/v8/funnel/' + b); }
function fetchSellOverbought() { return fetchJSON('/api/v8/sell_overbought?limit=50'); }
function fetchPositions()      { return fetchJSON('/api/paper/status'); }
function fetchRawMetrics()     { return fetchJSON('/api/v8/raw?limit=250'); }


// ════════════════════════════════════════════════════════════════════
//   REFRESH ENTRY POINTS
// ════════════════════════════════════════════════════════════════════

function refreshAll() {
  toast('Refreshing all tabs…');
  clearRawCache();
  refreshMasterDashboard();
  refreshSellOverbought();
  refreshInPosition();
  refreshTradeLog();
  refreshRawData();
  refreshFilterScan();
  const _raw = getRawMetricsCached();
  ['buy_reversal', 'buy_momentum'].forEach(b => refreshBasketFunnel(b, _raw));
  ['sell_reversal', 'sell_momentum'].forEach(b => refreshBasketFunnel(b, _raw));
  toast('✓ All tabs refreshed');
}

function refreshBuyBaskets() {
  toast('Refreshing buy baskets…');
  clearRawCache();
  const _raw = getRawMetricsCached();
  ['buy_reversal', 'buy_momentum'].forEach(b => refreshBasketFunnel(b, _raw));
  toast('✓ Buy baskets refreshed');
}

function refreshSellBaskets() {
  toast('Refreshing sell baskets…');
  clearRawCache();
  const _raw = getRawMetricsCached();
  ['sell_reversal', 'sell_momentum'].forEach(b => refreshBasketFunnel(b, _raw));
  toast('✓ Sell baskets refreshed');
}

function refreshSellFunnels() {
  toast('Refreshing sell funnels…');
  clearRawCache();
  const _raw = getRawMetricsCached();
  refreshBasketFunnel('sell_reversal', _raw);
  refreshBasketFunnel('sell_momentum', _raw);
  toast('✓ Sell funnels refreshed');
}

function buildAllTabs() {
  Object.values(SHEETS).forEach(name => getOrCreate(name));
  toast('All 10 tabs created. Run "Refresh All" next.');
}


// ════════════════════════════════════════════════════════════════════
//   SHARED HELPERS
// ════════════════════════════════════════════════════════════════════

function getOrCreate(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(name);
  if (!sheet) sheet = ss.insertSheet(name);
  return sheet;
}

function toast(msg) {
  SpreadsheetApp.getActiveSpreadsheet().toast(msg, 'Scorr V8', 3);
}

function nowIST() {
  return Utilities.formatDate(new Date(), 'Asia/Kolkata', 'd-MMM-yyyy HH:mm:ss');
}

function fmtNum(v, decimals) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (isNaN(n)) return v;
  return n.toFixed(decimals === undefined ? 2 : decimals);
}

function fmtPct(v) {
  if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '—';
  return Number(v).toFixed(2) + '%';
}

function fmtPnL(v) {
  if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '₹0';
  const n = Number(v);
  const sign = n >= 0 ? '' : '-';
  return sign + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN');
}

function parsePnL(s) {
  if (typeof s === 'number') return s;
  if (!s) return 0;
  const n = Number(String(s).replace(/[₹,]/g, '').replace(/[^\d.\-]/g, ''));
  return isNaN(n) ? 0 : n;
}

function fmtDate(d) {
  if (!d) return '—';
  try {
    return Utilities.formatDate(new Date(d), 'Asia/Kolkata', 'd MMM HH:mm');
  } catch (e) { return d; }
}

function humanLogic(metric) {
  const map = {
    'gvm_score':       'Quality gate',
    'year_return':     'Long-term trend (1Y c2c)',
    'dma_200':         'vs 200-day MA',
    'dma_50':          'vs 50-day MA',
    'dma_20':          'vs 20-day MA',
    'rsi_month':       'Monthly RSI (EOD-frozen)',
    'rsi_weekly':      'Weekly RSI (EOD-frozen)',
    'daily_rsi':       'Daily RSI (live)',
    'month_return':    'Monthly return (c2c)',
    'week_return':     'Weekly return (c2c)',
    'day_change':      '2-Day net change (c2c)',
    'sector_week':     'Sector week avg return (EOD-frozen)',
    'sector_month':    'Sector month avg return (EOD-frozen)',
    'sector_day':      'Sector today (live)',
    'month_index':     'Market breadth',
    'week_index_52':   '52-week position',
    'range_1d':        'Intraday H-L range',
    'range_3d':        '3-day H-L range',
    'ma9_vs_ma21':     'Short-term stretch',
    'vol_ratio':       'Volume vs avg',
  };
  return map[metric] || metric;
}

function renderSectionHeader(sheet, row, label, bg) {
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(label).setBackground(bg).setFontColor(COLORS.WHITE)
    .setFontSize(11).setFontWeight('bold').setVerticalAlignment('middle').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 28);
  return row + 1;
}

function renderSubHeader(sheet, row, label) {
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(label).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE)
    .setFontSize(10).setFontWeight('bold').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 24);
  return row + 1;
}

function renderTableHeader(sheet, row, headers, ncol) {
  headers.forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h)
      .setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG)
      .setFontSize(10).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center')
      .setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
  });
  return row + 1;
}

function renderDataRow(sheet, row, ncol, vals, opts) {
  opts = opts || {};
  vals.forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v)
      .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
      .setFontWeight(i === 0 ? 'bold' : 'normal')
      .setHorizontalAlignment(i === 0 ? 'left' : 'center')
      .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
    if (opts.pnlCols && opts.pnlCols.includes(i + 1)) {
      cell.setFontColor(parsePnL(v) >= 0 ? COLORS.PROFIT : COLORS.LOSS).setFontWeight('bold');
    }
  });
  sheet.getRange(row, 1, 1, ncol).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
  return row + 1;
}

function renderTotalRow(sheet, row, ncol, vals, opts) {
  opts = opts || {};
  vals.forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v)
      .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
      .setFontWeight('bold').setHorizontalAlignment(i === 0 ? 'left' : 'center')
      .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(11);
    if (opts.pnlCols && opts.pnlCols.includes(i + 1)) {
      cell.setFontColor(parsePnL(v) >= 0 ? '#86EFAC' : '#FCA5A5');
    }
  });
  sheet.setRowHeight(row, 28);
  return row + 1;
}


// ════════════════════════════════════════════════════════════════════
//   AGGREGATION HELPERS
// ════════════════════════════════════════════════════════════════════

const ALL_STRATS = ['Buy Reversal', 'Buy Momentum', 'Sell Reversal', 'Sell Momentum', 'Sell Overbought'];

function zeroAgg() {
  return { count: 0, pnl: 0, winning: 0, losing: 0, targetHit: 0, slGap: 0, accuracy: 0, avgPnl: 0 };
}

function normalizeBasket(basket) {
  const s = String(basket).toLowerCase();
  if (s.includes('buy')  && s.includes('rev')) return 'Buy Reversal';
  if (s.includes('buy')  && s.includes('mom')) return 'Buy Momentum';
  if (s.includes('sell') && s.includes('rev')) return 'Sell Reversal';
  if (s.includes('sell') && s.includes('mom')) return 'Sell Momentum';
  if (s.includes('over') || s.includes('ob'))  return 'Sell Overbought';
  return basket || 'Unknown';
}

function aggregatePositions(positions) {
  const out = {};
  ALL_STRATS.forEach(s => { out[s] = zeroAgg(); });
  positions.forEach(p => {
    const strat = normalizeBasket(p.basket || p.v8_basket || '');
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl = Number(p.unrealised_pnl || 0);
    out[strat].count++;
    out[strat].pnl += pnl;
    if (pnl > 0) out[strat].winning++;
    else if (pnl < 0) out[strat].losing++;
  });
  Object.keys(out).forEach(k => {
    const a = out[k];
    a.accuracy = (a.winning + a.losing) > 0 ? (a.winning / (a.winning + a.losing)) * 100 : 0;
    a.avgPnl   = a.count > 0 ? a.pnl / a.count : 0;
    a.pnl      = fmtPnL(a.pnl);
    a.avgPnl   = fmtPnL(a.avgPnl);
  });
  return out;
}

function aggregateTrades(trades) {
  const out = {};
  ALL_STRATS.forEach(s => { out[s] = zeroAgg(); });
  trades.forEach(t => {
    const strat  = normalizeBasket(t.basket || '');
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl    = Number(t.pnl || 0);
    const result = (t.result || '').toUpperCase();
    out[strat].count++;
    out[strat].pnl += pnl;
    if (result === 'TARGET') out[strat].targetHit++;
    else out[strat].slGap++;
  });
  Object.keys(out).forEach(k => {
    const a    = out[k];
    a.accuracy = a.count > 0 ? (a.targetHit / a.count) * 100 : 0;
    a.avgPnl   = a.count > 0 ? a.pnl / a.count : 0;
    a.pnl      = fmtPnL(a.pnl);
    a.avgPnl   = fmtPnL(a.avgPnl);
  });
  return out;
}

function sumAggRows(aggs) {
  const t = { count: 0, pnl: 0, winning: 0, losing: 0 };
  aggs.forEach(a => {
    t.count   += a.count;
    t.pnl     += parsePnL(a.pnl);
    t.winning += a.winning;
    t.losing  += a.losing;
  });
  t.accuracy = (t.winning + t.losing) > 0 ? (t.winning / (t.winning + t.losing)) * 100 : 0;
  t.avgPnl   = t.count > 0 ? t.pnl / t.count : 0;
  t.pnl      = fmtPnL(t.pnl);
  t.avgPnl   = fmtPnL(t.avgPnl);
  return t;
}

function sumClosedAggRows(aggs) {
  const t = { count: 0, pnl: 0, targetHit: 0, slGap: 0 };
  aggs.forEach(a => {
    t.count     += a.count;
    t.pnl       += parsePnL(a.pnl);
    t.targetHit += a.targetHit || 0;
    t.slGap     += a.slGap    || 0;
  });
  t.accuracy = t.count > 0 ? (t.targetHit / t.count) * 100 : 0;
  t.avgPnl   = t.count > 0 ? t.pnl / t.count : 0;
  t.pnl      = fmtPnL(t.pnl);
  t.avgPnl   = fmtPnL(t.avgPnl);
  return t;
}

function computeHolding(entryTime, exitTime) {
  if (!entryTime) return '—';
  try {
    const entry = new Date(entryTime);
    const exit  = exitTime ? new Date(exitTime) : new Date();
    const days  = Math.floor((exit - entry) / (1000 * 60 * 60 * 24));
    if (days === 0) return 'Intraday';
    if (days === 1) return '1 Day';
    return days + ' Days';
  } catch (e) { return '—'; }
}
