/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║                                                                  ║
 * ║   SCORR V8 — Quant Long-Short Tracker                            ║
 * ║   Front-end for Google Sheet "V8 Final CLS V"                    ║
 * ║                                                                  ║
 * ║   Tabs:                                                          ║
 * ║     1. Master_Dashboard       — Performance + Gate + Filters     ║
 * ║     2. Buy_Reversal           — Stock funnel breakdown           ║
 * ║     3. Buy_Momentum           — Stock funnel breakdown           ║
 * ║     4. Sell_Reversal          — Stock funnel breakdown           ║
 * ║     5. Sell_Momentum          — Stock funnel breakdown           ║
 * ║     6. Sell_Overbought        — Failed breakout signals          ║
 * ║     7. In_Position            — Live open trades (personal)      ║
 * ║     8. Trade_Log              — Closed trade history (personal)  ║
 * ║                                                                  ║
 * ║   Data source: Railway V8 endpoints + personal_journal table     ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */


// ═══════════════════════════════════════════════════════════════════════════════
//   CONFIG
// ═══════════════════════════════════════════════════════════════════════════════

const SCRIPT_VERSION = '1.2.4';   // fetchMetricsAll -> /api/v8/metrics/all (V5 fully removed)
const SCRIPT_RAW_URL = 'https://raw.githubusercontent.com/AGQuant/quant_project/main/apps_script/v8_dashboard.gs';

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
};

const FONTS = {
  TITLE:     { family: 'Inter',     size: 14, weight: 'bold' },
  SUBTITLE:  { family: 'Inter',     size: 11, weight: 'bold' },
  HEADER:    { family: 'Inter',     size: 10, weight: 'bold' },
  BODY:      { family: 'Inter',     size: 10, weight: 'normal' },
  MONO:      { family: 'Roboto Mono', size: 10, weight: 'normal' },
  BIG_NUM:   { family: 'Inter',     size: 18, weight: 'bold' },
};

const BASKET_META = {
  buy_reversal:    { label: 'Buy Reversal',    color: COLORS.BUY_REV,  emoji: '▲' },
  buy_momentum:    { label: 'Buy Momentum',    color: COLORS.BUY_MOM,  emoji: '▲' },
  sell_reversal:   { label: 'Sell Reversal',   color: COLORS.SELL_REV, emoji: '▼' },
  sell_momentum:   { label: 'Sell Momentum',   color: COLORS.SELL_MOM, emoji: '▼' },
  sell_overbought: { label: 'Sell Overbought', color: COLORS.SELL_OB,  emoji: '⚠' },
};

const SIGNAL_COLS = ['Symbol', 'GVM', 'DMA200 %', 'DMA50 %', 'RSI Month', 'RSI Week', 'Mth Ret %', 'Wk Ret %'];


// ═══════════════════════════════════════════════════════════════════════════════
//   MENU + TRIGGERS
// ═══════════════════════════════════════════════════════════════════════════════

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('🟣 Scorr V8')
    .addItem('🔄 Refresh All',              'refreshAll')
    .addSeparator()
    .addItem('📊 Refresh Dashboard',        'refreshMasterDashboard')
    .addItem('🔺 Refresh Buy Baskets',      'refreshBuyBaskets')
    .addItem('🔻 Refresh Sell Baskets',     'refreshSellBaskets')
    .addItem('⚠️  Refresh Sell Overbought', 'refreshSellOverbought')
    .addItem('📍 Refresh In Position',      'refreshInPosition')
    .addItem('📒 Refresh Trade Log',        'refreshTradeLog')
    .addSeparator()
    .addItem('🧹 Clean Rebuild (delete + rebuild + refresh)', 'cleanRebuild')
    .addSeparator()
    .addItem('🆕 Check for Updates',        'pullLatestFromGitHub')
    .addItem('🏗️  Build All Tabs (first run)', 'buildAllTabs')
    .addItem('⏰ Setup Auto-Refresh (5 min)',  'setupTriggers')
    .addItem('🛑 Stop Auto-Refresh',           'stopTriggers')
    .addItem('ℹ️  About / Version',            'showVersion')
    .addToUi();
}

function setupTriggers() {
  stopTriggers();
  ScriptApp.newTrigger('scheduledRefresh')
    .timeBased()
    .everyMinutes(5)
    .create();
  SpreadsheetApp.getActiveSpreadsheet().toast('Auto-refresh enabled — every 5 min', 'Scorr V8', 4);
}

function stopTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'scheduledRefresh') ScriptApp.deleteTrigger(t);
  });
}

function scheduledRefresh() {
  const ist = new Date(Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM-dd HH:mm:ss'));
  const day = ist.getDay();
  const hour = ist.getHours();
  const min = ist.getMinutes();
  if (day === 0 || day === 6) return;
  const minutes = hour * 60 + min;
  if (minutes < 555 || minutes > 930) return;
  refreshAll();
}


// ═══════════════════════════════════════════════════════════════════════════════
//   CLEAN REBUILD
// ═══════════════════════════════════════════════════════════════════════════════

function cleanRebuild() {
  const ui = SpreadsheetApp.getUi();
  const confirm = ui.alert(
    '🧹 Clean Rebuild',
    'This will DELETE all 8 Scorr tabs and rebuild them fresh from Railway.\n\nContinue?',
    ui.ButtonSet.YES_NO
  );
  if (confirm !== ui.Button.YES) return;

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  toast('Step 1/3 — Deleting old tabs…');

  let tempSheet = ss.getSheetByName('__temp__');
  if (!tempSheet) tempSheet = ss.insertSheet('__temp__');

  Object.values(SHEETS).forEach(name => {
    const s = ss.getSheetByName(name);
    if (s) ss.deleteSheet(s);
  });

  toast('Step 2/3 — Rebuilding tabs…');
  Object.values(SHEETS).forEach(name => ss.insertSheet(name));

  const tmp = ss.getSheetByName('__temp__');
  if (tmp) ss.deleteSheet(tmp);

  toast('Step 3/3 — Refreshing data from Railway…');
  refreshAll();
  setupTriggers();

  ss.toast('✅ Clean rebuild complete — auto-refresh restarted', 'Scorr V8', 5);
}


// ═══════════════════════════════════════════════════════════════════════════════
//   UPDATE CHECKER
// ═══════════════════════════════════════════════════════════════════════════════

function pullLatestFromGitHub() {
  let remoteCode, remoteVersion;
  try {
    const response = UrlFetchApp.fetch(SCRIPT_RAW_URL, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      SpreadsheetApp.getUi().alert('❌ GitHub fetch failed: HTTP ' + response.getResponseCode());
      return;
    }
    remoteCode = response.getContentText();
    const match = remoteCode.match(/const\s+SCRIPT_VERSION\s*=\s*['"]([^'"]+)['"]/);
    remoteVersion = match ? match[1] : 'unknown';
  } catch (e) {
    SpreadsheetApp.getUi().alert('❌ Could not reach GitHub:\n' + e);
    return;
  }

  if (remoteVersion === SCRIPT_VERSION) {
    SpreadsheetApp.getUi().alert(
      '✅ Up to date',
      `You're on the latest version.\n\nCurrent: ${SCRIPT_VERSION}\nRemote: ${remoteVersion}`,
      SpreadsheetApp.getUi().ButtonSet.OK
    );
    return;
  }

  const html = HtmlService.createHtmlOutput(
    `<!DOCTYPE html><html><head><style>
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; margin: 0; color: #1F2937; }
      h2 { margin-top: 0; color: #2563EB; }
      .row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #E5E7EB; }
      .label { color: #6B7280; }
      .val { font-family: 'Roboto Mono', monospace; font-weight: bold; }
      .new { color: #16A34A; }
      .url-box { background: #F3F4F6; padding: 10px; border-radius: 6px; font-family: 'Roboto Mono', monospace; font-size: 11px; word-break: break-all; margin: 12px 0; user-select: all; }
      button { background: #2563EB; color: white; border: none; padding: 12px 24px; border-radius: 6px; font-size: 14px; font-weight: bold; cursor: pointer; width: 100%; margin-top: 10px; }
      button:hover { background: #1D4ED8; }
      ol { padding-left: 20px; font-size: 13px; line-height: 1.6; color: #374151; }
      .steps { background: #FEF3C7; padding: 12px; border-radius: 6px; margin-top: 16px; }
    </style></head><body>
      <h2>🆕 New version available</h2>
      <div class="row"><span class="label">Installed</span><span class="val">${SCRIPT_VERSION}</span></div>
      <div class="row"><span class="label">Latest</span><span class="val new">${remoteVersion}</span></div>
      <p style="margin-top: 16px; margin-bottom: 4px;"><strong>Raw script URL:</strong></p>
      <div class="url-box" id="url">${SCRIPT_RAW_URL}</div>
      <button onclick="copyUrl()" id="btn">📋 Copy URL</button>
      <div class="steps">
        <strong>To install:</strong>
        <ol>
          <li>URL copied — open in new tab → Ctrl+A → Ctrl+C</li>
          <li>Apps Script editor → Ctrl+A → Ctrl+V</li>
          <li>Save (Ctrl+S) → reload this sheet</li>
        </ol>
      </div>
      <script>
        function copyUrl() {
          const text = ${JSON.stringify(SCRIPT_RAW_URL)};
          navigator.clipboard.writeText(text).then(
            () => { document.getElementById('btn').innerText = '✓ Copied to clipboard'; },
            () => {
              const r = document.createRange();
              r.selectNode(document.getElementById('url'));
              window.getSelection().removeAllRanges();
              window.getSelection().addRange(r);
              document.execCommand('copy');
              document.getElementById('btn').innerText = '✓ Copied (fallback)';
            }
          );
        }
      </script>
    </body></html>`
  ).setWidth(480).setHeight(420);

  SpreadsheetApp.getUi().showModalDialog(html, 'Scorr V8 Update Available');
}

function showVersion() {
  const html = HtmlService.createHtmlOutput(
    `<!DOCTYPE html><html><head><style>
      body { font-family: -apple-system, sans-serif; padding: 20px; color: #1F2937; }
      h2 { color: #9333EA; margin-top: 0; }
      .row { padding: 6px 0; }
      .label { color: #6B7280; display: inline-block; width: 120px; }
      .val { font-family: 'Roboto Mono', monospace; font-weight: bold; }
    </style></head><body>
      <h2>🟣 Scorr V8</h2>
      <div class="row"><span class="label">Version</span><span class="val">${SCRIPT_VERSION}</span></div>
      <div class="row"><span class="label">API base</span><span class="val">${BASE_URL}</span></div>
      <div class="row"><span class="label">Tabs</span><span class="val">${Object.keys(SHEETS).length}</span></div>
      <div class="row"><span class="label">Baskets</span><span class="val">${BASKETS.length}</span></div>
      <div class="row"><span class="label">Trade source</span><span class="val">personal_journal</span></div>
      <p style="margin-top: 20px; color: #6B7280; font-size: 12px;">Run <strong>🆕 Check for Updates</strong> to fetch latest version from GitHub.</p>
    </body></html>`
  ).setWidth(380).setHeight(280);
  SpreadsheetApp.getUi().showModalDialog(html, 'About');
}


// ═══════════════════════════════════════════════════════════════════════════════
//   MAIN REFRESH ENTRY POINTS
// ═══════════════════════════════════════════════════════════════════════════════

function refreshAll() {
  toast('Refreshing all tabs…');
  refreshMasterDashboard();
  BASKETS.slice(0, 4).forEach(refreshBasketFunnel);
  refreshSellOverbought();
  refreshInPosition();
  refreshTradeLog();
  toast('✓ All tabs refreshed');
}

function refreshBuyBaskets() {
  toast('Refreshing buy baskets…');
  ['buy_reversal', 'buy_momentum'].forEach(refreshBasketFunnel);
  toast('✓ Buy baskets refreshed');
}

function refreshSellBaskets() {
  toast('Refreshing sell baskets…');
  ['sell_reversal', 'sell_momentum'].forEach(refreshBasketFunnel);
  toast('✓ Sell baskets refreshed');
}


// ═══════════════════════════════════════════════════════════════════════════════
//   API CALLS
// ═══════════════════════════════════════════════════════════════════════════════

function fetchJSON(endpoint) {
  try {
    const response = UrlFetchApp.fetch(BASE_URL + endpoint, {
      muteHttpExceptions: true,
      headers: { 'Accept': 'application/json' },
    });
    const code = response.getResponseCode();
    if (code !== 200) {
      Logger.log(`API ${endpoint} returned ${code}: ${response.getContentText().slice(0, 200)}`);
      return null;
    }
    return JSON.parse(response.getContentText());
  } catch (e) {
    Logger.log(`fetchJSON ${endpoint} failed: ${e}`);
    return null;
  }
}

function fetchMarketMood()     { return fetchJSON('/api/v8/market_mood'); }
function fetchFilterConfig(b)  { return fetchJSON('/api/v8/filter_config/' + b); }
function fetchQualified(b)     { return fetchJSON('/api/v8/qualified/' + b + '?limit=200'); }
function fetchSellOverbought() { return fetchJSON('/api/v8/sell_overbought?limit=50'); }
function fetchPositions()      { return fetchJSON('/api/v8/positions?limit=100'); }
function fetchTrades()         { return fetchJSON('/api/v8/trades?limit=200'); }
function fetchMetricsAll()     { return fetchJSON('/api/v8/metrics/all'); }


// ═══════════════════════════════════════════════════════════════════════════════
//   TAB: MASTER DASHBOARD
// ═══════════════════════════════════════════════════════════════════════════════

function refreshMasterDashboard() {
  const sheet = getOrCreate(SHEETS.DASH);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);
  sheet.setColumnWidths(1, 10, 105);
  sheet.setColumnWidth(1, 165);

  let row = 1;
  row = renderTitleBar(sheet, row, 'SCORR V8 — MASTER DASHBOARD', 'Quant Long-Short Tracker');
  row++;

  row = renderSectionHeader(sheet, row, '🎯  TODAY\'S SIGNALS', COLORS.DARK_HEADER);
  row = renderTodaysSignals(sheet, row);
  row += 2;

  row = renderSectionHeader(sheet, row, '🚦  MARKET GATE', COLORS.DARK_HEADER);
  row = renderMarketGate(sheet, row);
  row += 2;

  row = renderSectionHeader(sheet, row, '📊  PERFORMANCE SUMMARY', COLORS.DARK_HEADER);
  row = renderPerformanceSummary(sheet, row);
  row += 2;

  row = renderSectionHeader(sheet, row, '⚙️  FILTER LOGIC (5 baskets)', COLORS.DARK_HEADER);
  row++;
  BASKETS.forEach(basket => {
    row = renderFilterCard(sheet, row, basket);
    row += 1;
  });

  sheet.setFrozenRows(2);
  toast('✓ Dashboard refreshed');
}


// ═══════════════════════════════════════════════════════════════════════════════
//   TODAY'S SIGNALS — consolidated across all 5 baskets
// ═══════════════════════════════════════════════════════════════════════════════

function renderTodaysSignals(sheet, row) {
  const allBasketData = {};
  let totalSignals = 0;

  BASKETS.forEach(basket => {
    const data = basket === 'sell_overbought'
      ? fetchSellOverbought()
      : fetchQualified(basket);
    allBasketData[basket] = data;
    totalSignals += (data && data.count) ? data.count : 0;
  });

  const bannerBg = totalSignals > 0 ? '#EFF6FF' : COLORS.NEUTRAL_BG;
  const bannerFg = totalSignals > 0 ? '#1E40AF' : COLORS.NEUTRAL_TEXT;
  sheet.getRange(row, 1, 1, 8).merge()
    .setValue(`${totalSignals} total signal${totalSignals !== 1 ? 's' : ''} today across 5 baskets   ·   Refreshed: ${nowIST()}`)
    .setBackground(bannerBg).setFontColor(bannerFg)
    .setFontSize(10).setFontWeight('bold')
    .setHorizontalAlignment('center');
  sheet.setRowHeight(row, 24);
  row++;

  row = renderTableHeader(sheet, row, SIGNAL_COLS, SIGNAL_COLS.length);

  BASKETS.forEach(basket => {
    const meta = BASKET_META[basket];
    const data = allBasketData[basket];
    const stocks = (data && (data.stocks || [])) || [];
    const count = (data && data.count) ? data.count : 0;

    sheet.getRange(row, 1, 1, SIGNAL_COLS.length).merge()
      .setValue(`${meta.emoji}  ${meta.label.toUpperCase()}   ·   ${count} stock${count !== 1 ? 's' : ''}`)
      .setBackground(meta.color).setFontColor(COLORS.WHITE)
      .setFontSize(10).setFontWeight('bold')
      .setHorizontalAlignment('left');
    sheet.setRowHeight(row, 22);
    row++;

    if (stocks.length === 0) {
      sheet.getRange(row, 1, 1, SIGNAL_COLS.length).merge()
        .setValue('No signals today')
        .setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT)
        .setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
      sheet.setRowHeight(row, 20);
      row++;
    } else {
      stocks.forEach(s => {
        const vals = [
          s.symbol,
          fmtNum(s.gvm_score, 2),
          fmtNum(s.dma_200, 2),
          fmtNum(s.dma_50, 2),
          fmtNum(s.rsi_month, 1),
          fmtNum(s.rsi_weekly, 1),
          fmtNum(s.month_return, 2),
          fmtNum(s.week_return, 2),
        ];
        vals.forEach((v, i) => {
          const cell = sheet.getRange(row, 1 + i);
          cell.setValue(v)
            .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
            .setFontWeight(i === 0 ? 'bold' : 'normal')
            .setFontColor(i === 0 ? meta.color : null)
            .setHorizontalAlignment(i === 0 ? 'left' : 'right')
            .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
        });
        sheet.getRange(row, 1, 1, SIGNAL_COLS.length)
          .setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
        row++;
      });
    }
  });

  sheet.setColumnWidth(1, 140);
  sheet.setColumnWidths(2, SIGNAL_COLS.length - 1, 95);
  return row;
}


function renderTitleBar(sheet, row, title, subtitle) {
  const range = sheet.getRange(row, 1, 1, 10).merge();
  range.setValue(title)
    .setBackground(COLORS.DARK_HEADER)
    .setFontColor(COLORS.WHITE)
    .setFontSize(16)
    .setFontWeight('bold')
    .setVerticalAlignment('middle')
    .setHorizontalAlignment('left');
  sheet.setRowHeight(row, 38);
  row++;

  const stamp = `${subtitle}   ·   v${SCRIPT_VERSION}   ·   Last refresh: ${nowIST()}`;
  const sub = sheet.getRange(row, 1, 1, 10).merge();
  sub.setValue(stamp)
    .setBackground(COLORS.SUBHEADER)
    .setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9)
    .setFontStyle('italic')
    .setHorizontalAlignment('left');
  sheet.setRowHeight(row, 22);
  return row + 1;
}


function renderSectionHeader(sheet, row, label, bg) {
  const range = sheet.getRange(row, 1, 1, 10).merge();
  range.setValue(label)
    .setBackground(bg)
    .setFontColor(COLORS.WHITE)
    .setFontSize(11)
    .setFontWeight('bold')
    .setVerticalAlignment('middle')
    .setHorizontalAlignment('left');
  sheet.setRowHeight(row, 28);
  return row + 1;
}


function renderPerformanceSummary(sheet, row) {
  const positions = fetchPositions() || [];
  const trades = fetchTrades() || [];

  const openAgg = aggregatePositions(positions);
  const closedAgg = aggregateTrades(trades);

  row = renderSubHeader(sheet, row, 'OPEN POSITIONS (Live)');
  const openHeaders = ['Strategy', 'Direction', 'Open', 'Unrealised P&L', 'Accuracy', 'Winning', 'Losing', 'Avg P&L'];
  row = renderTableHeader(sheet, row, openHeaders, 8);

  ['Buy Reversal', 'Buy Momentum', 'Sell Reversal', 'Sell Momentum'].forEach(strat => {
    const a = openAgg[strat] || zeroAgg();
    const dir = strat.startsWith('Buy') ? 'LONG' : 'SHORT';
    row = renderDataRow(sheet, row, 8, [
      strat, dir, a.count, a.pnl, fmtPct(a.accuracy), a.winning, a.losing, a.avgPnl,
    ], { pnlCols: [4, 8] });
  });
  const tot = openAgg.__TOTAL || zeroAgg();
  row = renderTotalRow(sheet, row, 8, ['TOTAL', 'ALL', tot.count, tot.pnl, fmtPct(tot.accuracy), tot.winning, tot.losing, tot.avgPnl], { pnlCols: [4, 8] });
  row++;

  row = renderSubHeader(sheet, row, 'CLOSED TRADES (Historical)');
  const closedHeaders = ['Strategy', 'Direction', 'Closed', 'Booked P&L', 'Accuracy', 'Target Hit', 'SL/Gate/Gap', 'Avg P&L'];
  row = renderTableHeader(sheet, row, closedHeaders, 8);

  ['Buy Reversal', 'Buy Momentum', 'Sell Reversal', 'Sell Momentum'].forEach(strat => {
    const a = closedAgg[strat] || zeroAgg();
    const dir = strat.startsWith('Buy') ? 'LONG' : 'SHORT';
    row = renderDataRow(sheet, row, 8, [
      strat, dir, a.count, a.pnl, fmtPct(a.accuracy), a.winning, a.losing, a.avgPnl,
    ], { pnlCols: [4, 8] });
  });
  const totC = closedAgg.__TOTAL || zeroAgg();
  row = renderTotalRow(sheet, row, 8, ['TOTAL', 'ALL', totC.count, totC.pnl, fmtPct(totC.accuracy), totC.targetHit, totC.slGap, totC.avgPnl], { pnlCols: [4, 8] });

  return row;
}


// ═══════════════════════════════════════════════════════════════════════════════
//   MARKET GATE — with slot tracking (used vs available)
// ═══════════════════════════════════════════════════════════════════════════════

function renderMarketGate(sheet, row) {
  const mood = fetchMarketMood();
  if (!mood) {
    sheet.getRange(row, 1, 1, 10).merge()
      .setValue('⚠ Could not fetch market mood — API unreachable')
      .setBackground(COLORS.FAIL_BG)
      .setFontColor(COLORS.FAIL_TEXT);
    return row + 1;
  }

  // Count open buy/sell positions from positions API
  const positions = fetchPositions() || [];
  const buyOpen  = positions.filter(p => isLongTrade(p)).length;
  const sellOpen = positions.filter(p => !isLongTrade(p)).length;

  const buySlots  = mood.buy_slots  || 0;
  const sellSlots = mood.sell_slots || 0;

  const buyRemaining  = Math.max(0, buySlots  - buyOpen);
  const sellRemaining = Math.max(0, sellSlots - sellOpen);

  const buyGateOpen  = buyRemaining  > 0;
  const sellGateOpen = sellRemaining > 0;

  // Filter checks table
  row = renderTableHeader(sheet, row, ['Filter', 'Live Value', 'Required', 'Pass/Fail'], 4);

  mood.checks.forEach(c => {
    const status = c.pass ? '✅ PASS' : '❌ FAIL';
    const bg = c.pass ? COLORS.PASS_BG : COLORS.FAIL_BG;
    const fg = c.pass ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT;

    sheet.getRange(row, 1).setValue(c.filter).setFontWeight('bold').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 2).setValue(c.value).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('right').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 3).setValue(c.required).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 4).setValue(status).setBackground(bg).setFontColor(fg).setFontWeight('bold').setHorizontalAlignment('center');
    sheet.getRange(row, 1, 1, 4).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
    row++;
  });

  row++;

  // Mood + total slots row
  sheet.getRange(row, 1).setValue('Mood').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 2).setValue(mood.mood).setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange(row, 3).setValue('Fails').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 4).setValue(mood.fails).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  sheet.getRange(row, 5).setValue('Total Slots').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 6).setValue('15').setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  sheet.setRowHeight(row, 24);
  row++;

  // BUY GATE row
  const buyBg = buyGateOpen ? COLORS.PASS_BG : COLORS.FAIL_BG;
  const buyFg = buyGateOpen ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT;
  const buyLabel = buyGateOpen ? '✅ BUY GATE OPEN' : '🔒 BUY GATE CLOSED';

  sheet.getRange(row, 1, 1, 2).merge()
    .setValue(buyLabel)
    .setBackground(buyBg).setFontColor(buyFg)
    .setFontSize(12).setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange(row, 3).setValue('Used').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 4).setValue(buyOpen)
    .setFontFamily(FONTS.MONO.family).setFontSize(13).setFontWeight('bold')
    .setHorizontalAlignment('center').setBackground(COLORS.BUY_REV).setFontColor(COLORS.WHITE);
  sheet.getRange(row, 5).setValue('Available').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 6).setValue(`${buyRemaining} / ${buySlots}`)
    .setFontFamily(FONTS.MONO.family).setFontSize(13).setFontWeight('bold')
    .setHorizontalAlignment('center').setBackground(buyBg).setFontColor(buyFg);
  sheet.setRowHeight(row, 30);
  row++;

  // SELL GATE row
  const sellBg = sellGateOpen ? COLORS.PASS_BG : COLORS.FAIL_BG;
  const sellFg = sellGateOpen ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT;
  const sellLabel = sellGateOpen ? '✅ SELL GATE OPEN' : '🔒 SELL GATE CLOSED';

  sheet.getRange(row, 1, 1, 2).merge()
    .setValue(sellLabel)
    .setBackground(sellBg).setFontColor(sellFg)
    .setFontSize(12).setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange(row, 3).setValue('Used').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 4).setValue(sellOpen)
    .setFontFamily(FONTS.MONO.family).setFontSize(13).setFontWeight('bold')
    .setHorizontalAlignment('center').setBackground(COLORS.SELL_REV).setFontColor(COLORS.WHITE);
  sheet.getRange(row, 5).setValue('Available').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
  sheet.getRange(row, 6).setValue(`${sellRemaining} / ${sellSlots}`)
    .setFontFamily(FONTS.MONO.family).setFontSize(13).setFontWeight('bold')
    .setHorizontalAlignment('center').setBackground(sellBg).setFontColor(sellFg);
  sheet.setRowHeight(row, 30);
  row++;

  return row;
}


function renderFilterCard(sheet, row, basket) {
  const meta = BASKET_META[basket];
  const config = fetchFilterConfig(basket);
  if (!config) return row;

  const header = sheet.getRange(row, 1, 1, 10).merge();
  header.setValue(`${meta.emoji}  ${meta.label.toUpperCase()}   ·   Target: ${config.target || 'S1'}   ·   Win%: ${config.win_pct || '—'}`)
    .setBackground(meta.color)
    .setFontColor(COLORS.WHITE)
    .setFontSize(11)
    .setFontWeight('bold')
    .setHorizontalAlignment('left');
  sheet.setRowHeight(row, 26);
  row++;

  row = renderTableHeader(sheet, row, ['Filter', 'Min', 'Max', 'Description'], 4);

  config.filters.forEach(f => {
    sheet.getRange(row, 1).setValue(f.metric).setFontWeight('bold').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 2).setValue(f.min_display).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 3).setValue(f.max_display).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 4).setValue(humanLogic(f.metric)).setFontColor(COLORS.NEUTRAL_TEXT).setFontStyle('italic').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 1, 1, 4).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
    row++;
  });

  return row;
}


// ═══════════════════════════════════════════════════════════════════════════════
//   TABS: BASKET FUNNELS
// ═══════════════════════════════════════════════════════════════════════════════

function refreshBasketFunnel(basket) {
  const sheetName = {
    buy_reversal: SHEETS.BR, buy_momentum: SHEETS.BM,
    sell_reversal: SHEETS.SR, sell_momentum: SHEETS.SM,
  }[basket];
  const sheet = getOrCreate(sheetName);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const meta = BASKET_META[basket];
  const config = fetchFilterConfig(basket);
  const qualified = fetchQualified(basket);
  const metricsAll = fetchMetricsAll() || [];

  if (!config || !qualified) {
    sheet.getRange(1, 1).setValue('⚠ API unreachable');
    return;
  }

  let row = 1;
  const titleRange = sheet.getRange(row, 1, 1, 14).merge();
  titleRange.setValue(`${meta.emoji}  ${meta.label.toUpperCase()} — Stock Funnel`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE)
    .setFontSize(15).setFontWeight('bold')
    .setHorizontalAlignment('left');
  sheet.setRowHeight(row, 34);
  row++;

  const stamp = sheet.getRange(row, 1, 1, 14).merge();
  stamp.setValue(`Universe: 290 F&O · Target: ${config.target || 'S1'} · Last refresh: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const filters = config.filters;
  const ncol = filters.length;

  sheet.getRange(row, 1).setValue('Filter').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE);
  filters.forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.metric).setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setWrap(true);
  });
  sheet.setRowHeight(row, 36);
  row++;

  sheet.getRange(row, 1).setValue('Min').setFontStyle('italic').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  filters.forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.min_display).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  });
  row++;

  sheet.getRange(row, 1).setValue('Max').setFontStyle('italic').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  filters.forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.max_display).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  });
  row += 2;

  const funnelCounts = computeFunnelCounts(metricsAll, filters);
  sheet.getRange(row, 1).setValue('Count').setFontWeight('bold').setBackground(meta.color).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  funnelCounts.forEach((c, i) => {
    sheet.getRange(row, 2 + i).setValue(c)
      .setFontWeight('bold').setBackground(meta.color).setFontColor(COLORS.WHITE)
      .setHorizontalAlignment('center').setFontSize(13);
  });
  sheet.setRowHeight(row, 30);
  row++;

  sheet.getRange(row, 1).setValue('QUALIFIED').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  const qcount = qualified.count || 0;
  sheet.getRange(row, 2, 1, ncol).merge()
    .setValue(`${qcount} stocks passed all filters`)
    .setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
    .setHorizontalAlignment('center').setFontSize(12);
  sheet.setRowHeight(row, 28);
  row += 2;

  sheet.getRange(row, 1).setValue('▼ QUALIFIED STOCKS').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE);
  sheet.getRange(row, 1, 1, ncol + 1).setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE);
  row++;

  row = renderTableHeader(sheet, row, SIGNAL_COLS, SIGNAL_COLS.length);

  const stocks = qualified.stocks || [];
  if (stocks.length === 0) {
    sheet.getRange(row, 1, 1, SIGNAL_COLS.length).merge()
      .setValue('No qualified stocks today')
      .setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT)
      .setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
    row++;
  } else {
    stocks.forEach(s => {
      const vals = [
        s.symbol,
        fmtNum(s.gvm_score, 2),
        fmtNum(s.dma_200, 2),
        fmtNum(s.dma_50, 2),
        fmtNum(s.rsi_month, 1),
        fmtNum(s.rsi_weekly, 1),
        fmtNum(s.month_return, 2),
        fmtNum(s.week_return, 2),
      ];
      vals.forEach((v, i) => {
        sheet.getRange(row, 1 + i).setValue(v)
          .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 0 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 0 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
      });
      sheet.getRange(row, 1, 1, SIGNAL_COLS.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }

  sheet.setColumnWidth(1, 140);
  sheet.setColumnWidths(2, SIGNAL_COLS.length, 95);
  sheet.setFrozenRows(3);

  toast(`✓ ${meta.label} refreshed`);
}


function computeFunnelCounts(metricsAll, filters) {
  const counts = [];
  let universe = metricsAll.slice();
  filters.forEach(f => {
    universe = universe.filter(stock => {
      const v = stock[f.metric];
      if (v === null || v === undefined) return false;
      if (f.min !== null && f.min !== undefined && v < f.min) return false;
      if (f.max !== null && f.max !== undefined && v > f.max) return false;
      return true;
    });
    counts.push(universe.length);
  });
  return counts;
}


// ═══════════════════════════════════════════════════════════════════════════════
//   TAB: SELL OVERBOUGHT
// ═══════════════════════════════════════════════════════════════════════════════

function refreshSellOverbought() {
  const sheet = getOrCreate(SHEETS.SO);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const data = fetchSellOverbought();
  const meta = BASKET_META.sell_overbought;

  let row = 1;

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`${meta.emoji}  SELL OVERBOUGHT — Failed Breakout / Exhaustion Reversal`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE)
    .setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  if (!data) {
    sheet.getRange(row, 1).setValue('⚠ API unreachable');
    return;
  }

  const subtitle = `Target: ${data.target || 'S1'} · SL: ${data.sl || '1:1'} · Backtest May-26: ${data.win_pct_may2026 || '71.4%'} · Refreshed: ${nowIST()}`;
  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(subtitle)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row++;

  const note = data.note || 'Market gate required — fails in recovery/bull markets';
  sheet.getRange(row, 1, 1, 14).merge()
    .setValue('ℹ ' + note)
    .setBackground('#FEF3C7').setFontColor('#92400E')
    .setFontStyle('italic').setFontSize(10);
  sheet.setRowHeight(row, 24);
  row += 2;

  row = renderSubHeader(sheet, row, 'FILTER LOGIC');
  const filterRows = [
    ['DMA 200',         '≥ 10%',    'Extended above 200-day MA'],
    ['52-week index',   '≥ 80',     'Near 52-week high'],
    ['MA9 vs MA21',     '≥ 3%',     'Short-term momentum stretched'],
    ['Volume ratio',    '≤ 0.8',    'Volume drying — distribution starting'],
    ['Range 1D',        '< 0',      'Today red — reversal trigger'],
    ['RSI Month',       '≥ 60',     'RSI elevated'],
  ];
  row = renderTableHeader(sheet, row, ['Filter', 'Threshold', 'Logic'], 3);
  filterRows.forEach(r => {
    sheet.getRange(row, 1).setValue(r[0]).setFontWeight('bold').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 2).setValue(r[1]).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 3).setValue(r[2]).setFontColor(COLORS.NEUTRAL_TEXT).setFontStyle('italic').setBackground(COLORS.CARD_BG);
    sheet.getRange(row, 1, 1, 3).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
    row++;
  });
  row += 2;

  row = renderSubHeader(sheet, row, `LIVE SIGNALS (${data.count || 0} qualified)`);
  const headers = ['Symbol', 'Entry', 'Target (S1)', 'Stop', 'Tgt %', 'DMA200', 'wi52', 'ma9_21', 'Vol Ratio', 'RSI M', 'Sector Wk'];
  row = renderTableHeader(sheet, row, headers, headers.length);

  const stocks = data.stocks || [];
  if (stocks.length === 0) {
    sheet.getRange(row, 1, 1, headers.length).merge()
      .setValue('No qualified Sell Overbought signals today')
      .setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT)
      .setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
  } else {
    stocks.forEach(s => {
      const vals = [
        s.symbol, fmtNum(s.entry, 2), fmtNum(s.target, 2), fmtNum(s.stop, 2),
        fmtPct(s.tgt_pct), fmtPct(s.dma_200), fmtNum(s.week_index_52, 1),
        fmtPct(s.ma9_vs_ma21), fmtNum(s.vol_ratio, 2), fmtNum(s.rsi_month, 1), fmtPct(s.sector_week),
      ];
      vals.forEach((v, i) => {
        sheet.getRange(row, 1 + i).setValue(v)
          .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 0 ? 'bold' : 'normal')
          .setFontColor(i === 0 ? meta.color : null)
          .setHorizontalAlignment(i === 0 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
      });
      sheet.getRange(row, 1, 1, headers.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }

  sheet.setColumnWidth(1, 140);
  sheet.setColumnWidths(2, headers.length - 1, 100);
  sheet.setFrozenRows(2);
  toast('✓ Sell Overbought refreshed');
}


// ═══════════════════════════════════════════════════════════════════════════════
//   TAB: IN POSITION
// ═══════════════════════════════════════════════════════════════════════════════

function refreshInPosition() {
  const sheet = getOrCreate(SHEETS.POS);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const positions = fetchPositions() || [];
  const mood = fetchMarketMood();

  let row = 1;
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue('📍  IN POSITION — LIVE OPEN TRADES')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
    .setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  const gateText = mood
    ? `Gate: ${mood.fails === 0 ? '✅ OPEN' : '❌ CLOSED'}   |   Buy: ${mood.buy_slots}   |   Sell: ${mood.sell_slots}   |   Max: 15   |   Refreshed: ${nowIST()}`
    : `Refreshed: ${nowIST()}`;
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(gateText)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(10);
  sheet.setRowHeight(row, 22);
  row += 2;

  const grouped = groupByStrategy(positions);
  ['Buy Reversal', 'Buy Momentum', 'Sell Reversal', 'Sell Momentum'].forEach(strat => {
    row = renderStrategyPositionSection(sheet, row, strat, grouped[strat] || []);
    row += 1;
  });

  row = renderSectionHeader(sheet, row, '📊  OVERALL OPEN SUMMARY', COLORS.DARK_HEADER);
  const long = positions.filter(p => isLongTrade(p));
  const short = positions.filter(p => !isLongTrade(p));
  const summary = [
    ['Total Open', positions.length, 'Long Open', long.length, 'Short Open', short.length],
    ['Total P&L', fmtPnL(sumPnl(positions)), 'Long P&L', fmtPnL(sumPnl(long)), 'Short P&L', fmtPnL(sumPnl(short))],
  ];
  summary.forEach(srow => {
    for (let i = 0; i < srow.length; i++) {
      const cell = sheet.getRange(row, 1 + i);
      i % 2 === 0
        ? cell.setValue(srow[i]).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right')
        : cell.setValue(srow[i]).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center');
    }
    row++;
  });

  sheet.setColumnWidth(1, 130);
  sheet.setColumnWidths(2, 9, 110);
  sheet.setFrozenRows(2);
  toast('✓ In Position refreshed');
}


function renderStrategyPositionSection(sheet, row, strategy, trades) {
  const meta = strategyMeta(strategy);
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`${meta.emoji}  ${strategy.toUpperCase()}`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE)
    .setFontSize(11).setFontWeight('bold');
  sheet.setRowHeight(row, 26);
  row++;

  const stats = computePositionStats(trades);
  ['Open Trades', 'Unrealised P&L', 'Accuracy', 'Winning', 'Losing', 'Avg P&L/Trade'].forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setFontSize(9).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center');
  });
  row++;

  [stats.count, fmtPnL(stats.totalPnl), fmtPct(stats.accuracy), stats.winning, stats.losing, fmtPnL(stats.avgPnl)].forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center').setFontSize(11);
    if (i === 1) cell.setFontColor(stats.totalPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
    if (i === 5) cell.setFontColor(stats.avgPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
  });
  sheet.setRowHeight(row, 24);
  row++;

  row = renderTableHeader(sheet, row, ['Entry Time', 'Stock', 'Entry', 'CMP', 'Qty', 'SL', 'Target', 'Unrealised P&L', 'Holding'], 9);

  if (trades.length === 0) {
    sheet.getRange(row, 1, 1, 9).merge().setValue('No open positions').setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
    row++;
  } else {
    trades.forEach(t => {
      const pnl = computeUnrealisedPnl(t);
      const vals = [
        fmtDate(t.entry_time), t.stock || t.symbol,
        fmtNum(t.entry_price, 2), fmtNum(t.cmp, 2), t.qty,
        fmtNum(t.sl, 2), fmtNum(t.target, 2), fmtPnL(pnl),
        computeHolding(t.entry_time),
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v)
          .setFontFamily(i === 1 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 1 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 1 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
        if (i === 7) cell.setFontColor(pnl >= 0 ? COLORS.PROFIT : COLORS.LOSS).setFontWeight('bold');
      });
      sheet.getRange(row, 1, 1, 9).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }
  return row;
}


// ═══════════════════════════════════════════════════════════════════════════════
//   TAB: TRADE LOG
// ═══════════════════════════════════════════════════════════════════════════════

function refreshTradeLog() {
  const sheet = getOrCreate(SHEETS.LOG);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const trades = fetchTrades() || [];
  let row = 1;

  sheet.getRange(row, 1, 1, 10).merge()
    .setValue('📒  TRADE LOG — CLOSED TRADES')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
    .setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`Last refresh: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const grouped = groupByStrategy(trades);
  ['Buy Reversal', 'Buy Momentum', 'Sell Reversal', 'Sell Momentum'].forEach(strat => {
    row = renderStrategyTradeSection(sheet, row, strat, grouped[strat] || []);
    row += 1;
  });

  row = renderSectionHeader(sheet, row, '📊  OVERALL CLOSED SUMMARY', COLORS.DARK_HEADER);
  const s = computeClosedStats(trades);
  [
    ['Total Closed', s.count, 'Overall Accuracy', fmtPct(s.accuracy)],
    ['Total Booked', fmtPnL(s.pnl), 'Target Hit', s.targetHit],
    ['SL Hit', s.slHit, 'Gate Exit', s.gateExit],
  ].forEach(srow => {
    for (let i = 0; i < srow.length; i++) {
      const cell = sheet.getRange(row, 1 + i);
      i % 2 === 0
        ? cell.setValue(srow[i]).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right')
        : cell.setValue(srow[i]).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center');
    }
    row++;
  });

  sheet.setColumnWidth(1, 130);
  sheet.setColumnWidths(2, 9, 110);
  sheet.setFrozenRows(2);
  toast('✓ Trade Log refreshed');
}


function renderStrategyTradeSection(sheet, row, strategy, trades) {
  const meta = strategyMeta(strategy);
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`${meta.emoji}  ${strategy.toUpperCase()}`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE)
    .setFontSize(11).setFontWeight('bold');
  sheet.setRowHeight(row, 26);
  row++;

  const stats = computeClosedStatsForGroup(trades);
  ['Closed Trades', 'Booked P&L', 'Accuracy', 'Target Hit', 'SL/Gate/Gap', 'Avg P&L/Trade'].forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setFontSize(9).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center');
  });
  row++;

  [stats.count, fmtPnL(stats.pnl), fmtPct(stats.accuracy), stats.targetHit, stats.slGap, fmtPnL(stats.avgPnl)].forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center').setFontSize(11);
    if (i === 1) cell.setFontColor(stats.pnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
    if (i === 5) cell.setFontColor(stats.avgPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
  });
  sheet.setRowHeight(row, 24);
  row++;

  row = renderTableHeader(sheet, row, ['Entry Time', 'Exit Time', 'Stock', 'Entry', 'Exit', 'P&L', 'Holding', 'Result'], 8);

  if (trades.length === 0) {
    sheet.getRange(row, 1, 1, 8).merge().setValue('No closed trades for this strategy').setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
    row++;
  } else {
    trades.forEach(t => {
      const pnl = computeClosedPnl(t);
      const result = t.Result || t.result || 'Unknown';
      const vals = [
        fmtDate(t.entry_time), fmtDate(t.exit_time), t.stock || t.symbol,
        fmtNum(t.entry, 2), fmtNum(t.exit, 2), fmtPnL(pnl),
        computeHolding(t.entry_time, t.exit_time), result,
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v)
          .setFontFamily(i === 2 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 2 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 2 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
        if (i === 5) cell.setFontColor(pnl >= 0 ? COLORS.PROFIT : COLORS.LOSS).setFontWeight('bold');
        if (i === 7) {
          const isWin = result.toLowerCase().includes('target');
          cell.setFontColor(isWin ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT)
            .setBackground(isWin ? COLORS.PASS_BG : COLORS.FAIL_BG)
            .setFontWeight('bold').setHorizontalAlignment('center');
        }
      });
      sheet.getRange(row, 1, 1, 8).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }
  return row;
}


// ═══════════════════════════════════════════════════════════════════════════════
//   BUILD ALL TABS
// ═══════════════════════════════════════════════════════════════════════════════

function buildAllTabs() {
  Object.values(SHEETS).forEach(name => getOrCreate(name));
  toast('All 8 tabs created. Run "Refresh All" next.');
}


// ═══════════════════════════════════════════════════════════════════════════════
//   HELPERS — AGGREGATIONS
// ═══════════════════════════════════════════════════════════════════════════════

function aggregatePositions(positions) {
  const out = { __TOTAL: zeroAgg() };
  positions.forEach(p => {
    const strat = inferStrategy(p);
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl = computeUnrealisedPnl(p);
    out[strat].count++; out[strat].pnl += pnl;
    if (pnl > 0) out[strat].winning++; else if (pnl < 0) out[strat].losing++;
    out.__TOTAL.count++; out.__TOTAL.pnl += pnl;
    if (pnl > 0) out.__TOTAL.winning++; else if (pnl < 0) out.__TOTAL.losing++;
  });
  Object.keys(out).forEach(k => {
    const a = out[k];
    a.accuracy = (a.winning + a.losing) > 0 ? (a.winning / (a.winning + a.losing)) * 100 : 0;
    a.avgPnl = a.count > 0 ? a.pnl / a.count : 0;
    a.pnl = fmtPnL(a.pnl); a.avgPnl = fmtPnL(a.avgPnl);
  });
  return out;
}

function aggregateTrades(trades) {
  const out = { __TOTAL: zeroAgg() };
  trades.forEach(t => {
    const strat = inferStrategy(t);
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl = computeClosedPnl(t);
    const result = (t.Result || t.result || '').toLowerCase();
    out[strat].count++; out[strat].pnl += pnl;
    if (result.includes('target')) out[strat].targetHit++; else out[strat].slGap++;
    out.__TOTAL.count++; out.__TOTAL.pnl += pnl;
    if (result.includes('target')) out.__TOTAL.targetHit++; else out.__TOTAL.slGap++;
  });
  Object.keys(out).forEach(k => {
    const a = out[k];
    a.accuracy = a.count > 0 ? (a.targetHit / a.count) * 100 : 0;
    a.avgPnl = a.count > 0 ? a.pnl / a.count : 0;
    a.pnl = fmtPnL(a.pnl); a.avgPnl = fmtPnL(a.avgPnl);
  });
  return out;
}

function zeroAgg() {
  return { count: 0, pnl: 0, winning: 0, losing: 0, targetHit: 0, slGap: 0, accuracy: 0, avgPnl: 0 };
}

function groupByStrategy(rows) {
  const out = { 'Buy Reversal': [], 'Buy Momentum': [], 'Sell Reversal': [], 'Sell Momentum': [] };
  rows.forEach(r => { const s = inferStrategy(r); if (out[s]) out[s].push(r); });
  return out;
}

function inferStrategy(row) {
  const explicit = row.Strategy || row.strategy || row.signal_type || row.v8_basket;
  if (explicit) {
    const s = String(explicit).toLowerCase();
    if (s.includes('buy') && s.includes('rev')) return 'Buy Reversal';
    if (s.includes('buy') && s.includes('mom')) return 'Buy Momentum';
    if (s.includes('sell') && s.includes('rev')) return 'Sell Reversal';
    if (s.includes('sell') && s.includes('mom')) return 'Sell Momentum';
    if (s.includes('overbought')) return 'Sell Reversal';
  }
  return isLongTrade(row) ? 'Buy Reversal' : 'Sell Reversal';
}

function isLongTrade(row) {
  const d = String(row.Direction || row.direction || row.Type || row.type || '').toUpperCase();
  if (d === 'LONG' || d === 'BUY') return true;
  if (d === 'SHORT' || d === 'SELL') return false;
  return true;
}

function computeUnrealisedPnl(p) {
  const explicit = p['Unrealised P&L'] || p.unrealised_pnl || p.pnl;
  if (explicit !== undefined && explicit !== null && explicit !== '') return Number(explicit) || 0;
  const entry = Number(p.entry_price || p.entry || 0);
  const cmp = Number(p.cmp || 0);
  const qty = Number(p.qty || 0);
  return isLongTrade(p) ? (cmp - entry) * qty : (entry - cmp) * qty;
}

function computeClosedPnl(t) {
  const explicit = t['P&L'] || t.pnl || t.booked_pnl;
  if (explicit !== undefined && explicit !== null && explicit !== '') return Number(explicit) || 0;
  const entry = Number(t.entry || 0);
  const exit = Number(t.exit || 0);
  const qty = Number(t.qty || 0);
  return isLongTrade(t) ? (exit - entry) * qty : (entry - exit) * qty;
}

function sumPnl(arr) { return arr.reduce((s, x) => s + computeUnrealisedPnl(x), 0); }

function computePositionStats(trades) {
  const s = { count: trades.length, totalPnl: 0, winning: 0, losing: 0 };
  trades.forEach(t => {
    const pnl = computeUnrealisedPnl(t); s.totalPnl += pnl;
    if (pnl > 0) s.winning++; else if (pnl < 0) s.losing++;
  });
  s.accuracy = (s.winning + s.losing) > 0 ? (s.winning / (s.winning + s.losing)) * 100 : 0;
  s.avgPnl = s.count > 0 ? s.totalPnl / s.count : 0;
  return s;
}

function computeClosedStats(trades) {
  const s = { count: trades.length, pnl: 0, targetHit: 0, slHit: 0, gateExit: 0, gapExit: 0 };
  trades.forEach(t => {
    s.pnl += computeClosedPnl(t);
    const r = String(t.Result || t.result || '').toLowerCase();
    if (r.includes('target')) s.targetHit++;
    else if (r.includes('sl')) s.slHit++;
    else if (r.includes('gate')) s.gateExit++;
    else if (r.includes('gap')) s.gapExit++;
  });
  s.accuracy = s.count > 0 ? (s.targetHit / s.count) * 100 : 0;
  return s;
}

function computeClosedStatsForGroup(trades) {
  const s = { count: trades.length, pnl: 0, targetHit: 0, slGap: 0 };
  trades.forEach(t => {
    s.pnl += computeClosedPnl(t);
    String(t.Result || t.result || '').toLowerCase().includes('target') ? s.targetHit++ : s.slGap++;
  });
  s.accuracy = s.count > 0 ? (s.targetHit / s.count) * 100 : 0;
  s.avgPnl = s.count > 0 ? s.pnl / s.count : 0;
  return s;
}

function computeHolding(entryTime, exitTime) {
  if (!entryTime) return '—';
  try {
    const entry = new Date(entryTime);
    const exit = exitTime ? new Date(exitTime) : new Date();
    const days = Math.floor((exit - entry) / 86400000);
    if (days === 0) return 'Intraday';
    if (days === 1) return '1 Day';
    return days + ' Days';
  } catch (e) { return '—'; }
}

function strategyMeta(strategy) {
  if (strategy === 'Buy Reversal')  return { color: COLORS.BUY_REV,  emoji: '▲' };
  if (strategy === 'Buy Momentum')  return { color: COLORS.BUY_MOM,  emoji: '▲' };
  if (strategy === 'Sell Reversal') return { color: COLORS.SELL_REV, emoji: '▼' };
  if (strategy === 'Sell Momentum') return { color: COLORS.SELL_MOM, emoji: '▼' };
  return { color: COLORS.DARK_HEADER, emoji: '•' };
}


// ═══════════════════════════════════════════════════════════════════════════════
//   HELPERS — UI / FORMATTING
// ═══════════════════════════════════════════════════════════════════════════════

function getOrCreate(name) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(name);
  if (!sheet) sheet = ss.insertSheet(name);
  return sheet;
}

function renderSubHeader(sheet, row, label) {
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(label)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE)
    .setFontSize(10).setFontWeight('bold').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 24);
  return row + 1;
}

function renderTableHeader(sheet, row, headers, ncol) {
  headers.forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h)
      .setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG)
      .setFontSize(10).setFontColor(COLORS.NEUTRAL_TEXT)
      .setHorizontalAlignment('center')
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
      .setFontWeight('bold')
      .setHorizontalAlignment(i === 0 ? 'left' : 'center')
      .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(11);
    if (opts.pnlCols && opts.pnlCols.includes(i + 1)) {
      cell.setFontColor(parsePnL(v) >= 0 ? '#86EFAC' : '#FCA5A5');
    }
  });
  sheet.setRowHeight(row, 28);
  return row + 1;
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
  return (n < 0 ? '-' : '') + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN');
}

function parsePnL(s) {
  if (typeof s === 'number') return s;
  if (!s) return 0;
  const n = Number(String(s).replace(/[₹,]/g, '').replace(/[^\d.\-]/g, ''));
  return isNaN(n) ? 0 : n;
}

function fmtDate(d) {
  if (!d) return '—';
  try { return Utilities.formatDate(new Date(d), 'Asia/Kolkata', 'd MMM HH:mm'); }
  catch (e) { return d; }
}

function nowIST() {
  return Utilities.formatDate(new Date(), 'Asia/Kolkata', 'd-MMM-yyyy HH:mm:ss');
}

function toast(msg) {
  SpreadsheetApp.getActiveSpreadsheet().toast(msg, 'Scorr V8', 3);
}

function humanLogic(metric) {
  const map = {
    'gvm_score': 'Quality gate', 'year_return': 'Long-term trend',
    'dma_200': 'vs 200-day MA', 'dma_50': 'vs 50-day MA', 'dma_20': 'vs 20-day MA',
    'rsi_month': 'Monthly RSI', 'rsi_weekly': 'Weekly RSI', 'daily_rsi': 'Daily RSI',
    'month_return': 'Monthly return', 'week_return': 'Weekly return',
    'sector_week': 'Sector week trend', 'sector_day': 'Sector today',
    'month_index': 'Market breadth', 'week_index_52': '52-week position',
    'range_1d': "Today's candle", 'range_3d': '3-day move',
    'ma9_vs_ma21': 'Short-term stretch', 'vol_ratio': 'Volume drying',
  };
  return map[metric] || metric;
}
