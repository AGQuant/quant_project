/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║   SCORR V8 — Tabs: All Sheet Renderers                          ║
 * ║   Part 2 of 2  →  v8_dashboard_tabs.gs                          ║
 * ║   Requires    →  v8_dashboard_core.gs                           ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */


// ════════════════════════════════════════════════════════════════════
//   TAB: MASTER DASHBOARD
// ════════════════════════════════════════════════════════════════════

function refreshMasterDashboard() {
  const sheet = getOrCreate(SHEETS.DASH);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);
  sheet.setColumnWidths(1, 10, 105);
  sheet.setColumnWidth(1, 165);

  let row = 1;
  row = renderTitleBar(sheet, row, 'SCORR V8 — MASTER DASHBOARD', 'Quant Long-Short Tracker');
  row++;

  row = renderSectionHeader(sheet, row, '📊  PERFORMANCE SUMMARY', COLORS.DARK_HEADER);
  row = renderPerformanceSummary(sheet, row);
  row += 2;

  row = renderSectionHeader(sheet, row, '🚦  MARKET GATE', COLORS.DARK_HEADER);
  row = renderMarketGate(sheet, row);
  row += 2;

  row = renderSectionHeader(sheet, row, '🎯  FILTER LOGIC (5 baskets)', COLORS.DARK_HEADER);
  row++;
  BASKETS.forEach(basket => { row = renderFilterCard(sheet, row, basket); row += 1; });

  sheet.setFrozenRows(2);
  toast('✓ Dashboard refreshed');
}

function renderTitleBar(sheet, row, title, subtitle) {
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(title).setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
    .setFontSize(16).setFontWeight('bold').setVerticalAlignment('middle').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 38);
  row++;
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`${subtitle}   ·   v${SCRIPT_VERSION}   ·   Last refresh: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9).setFontStyle('italic').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 22);
  return row + 1;
}

function renderPerformanceSummary(sheet, row) {
  const posData   = fetchPositions() || {};
  const positions = posData.open_positions || [];
  const trades    = posData.recent_trades  || [];
  const openAgg   = aggregatePositions(positions);
  const closedAgg = aggregateTrades(trades);

  row = renderSubHeader(sheet, row, 'OPEN POSITIONS (Live)');
  row = renderTableHeader(sheet, row, ['Strategy', 'Direction', 'Open', 'Unrealised P&L', 'Accuracy', 'Winning', 'Losing', 'Avg P&L'], 8);

  const openRows = [
    ['Buy Reversal',    'LONG'],
    ['Buy Momentum',    'LONG'],
    ['Sell Reversal',   'SHORT'],
    ['Sell Momentum',   'SHORT'],
    ['Sell Overbought', 'SHORT'],
  ];
  openRows.forEach(([strat, dir]) => {
    const a = openAgg[strat] || zeroAgg();
    row = renderDataRow(sheet, row, 8,
      [strat, dir, a.count, a.pnl, fmtPct(a.accuracy), a.winning, a.losing, a.avgPnl],
      { pnlCols: [4, 8] });
  });
  const totO = sumAggRows(openRows.map(([s]) => openAgg[s] || zeroAgg()));
  row = renderTotalRow(sheet, row, 8,
    ['TOTAL', 'ALL', totO.count, totO.pnl, fmtPct(totO.accuracy), totO.winning, totO.losing, totO.avgPnl],
    { pnlCols: [4, 8] });
  row++;

  row = renderSubHeader(sheet, row, 'CLOSED TRADES (Historical)');
  row = renderTableHeader(sheet, row, ['Strategy', 'Direction', 'Closed', 'Booked P&L', 'Accuracy', 'Target Hit', 'SL/Gate/Gap', 'Avg P&L'], 8);

  const closedRows = [
    ['Buy Reversal',    'LONG'],
    ['Buy Momentum',    'LONG'],
    ['Sell Reversal',   'SHORT'],
    ['Sell Momentum',   'SHORT'],
    ['Sell Overbought', 'SHORT'],
  ];
  closedRows.forEach(([strat, dir]) => {
    const a = closedAgg[strat] || zeroAgg();
    row = renderDataRow(sheet, row, 8,
      [strat, dir, a.count, a.pnl, fmtPct(a.accuracy), a.targetHit, a.slGap, a.avgPnl],
      { pnlCols: [4, 8] });
  });
  const totC = sumClosedAggRows(closedRows.map(([s]) => closedAgg[s] || zeroAgg()));
  row = renderTotalRow(sheet, row, 8,
    ['TOTAL', 'ALL', totC.count, totC.pnl, fmtPct(totC.accuracy), totC.targetHit, totC.slGap, totC.avgPnl],
    { pnlCols: [4, 8] });

  return row;
}

function renderMarketGate(sheet, row) {
  const mood = fetchMarketMood();
  if (!mood) {
    sheet.getRange(row, 1, 1, 10).merge()
      .setValue('⚠ Could not fetch market mood — API unreachable')
      .setBackground(COLORS.FAIL_BG).setFontColor(COLORS.FAIL_TEXT);
    return row + 1;
  }

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

  const gateText = mood.fails === 0 ? '✅ GATE OPEN' : '❌ GATE CLOSED';
  const gateBg   = mood.fails === 0 ? COLORS.PASS_BG : COLORS.FAIL_BG;
  const gateFg   = mood.fails === 0 ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT;
  sheet.getRange(row, 1, 1, 2).merge().setValue(gateText)
    .setBackground(gateBg).setFontColor(gateFg).setFontSize(13).setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange(row, 3).setValue(`${mood.fails} failing · ${mood.mood}`).setFontStyle('italic').setHorizontalAlignment('center');
  sheet.setRowHeight(row, 28);
  row += 2;

  const posData   = fetchPositions() || {};
  const openPos   = posData.open_positions || [];
  const longOpen  = openPos.filter(p => (p.side || '').toUpperCase() === 'LONG').length;
  const shortOpen = openPos.filter(p => (p.side || '').toUpperCase() === 'SHORT').length;
  const buyTotal  = mood.buy_slots  || 0;
  const sellTotal = mood.sell_slots || 0;
  const buyAvail  = Math.max(0, buyTotal  - longOpen);
  const sellAvail = Math.max(0, sellTotal - shortOpen);

  ['', 'TOTAL SLOTS', 'FILLED', 'AVAILABLE'].forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h)
      .setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
      .setHorizontalAlignment('center').setFontSize(10);
  });
  sheet.setRowHeight(row, 24);
  row++;

  sheet.getRange(row, 1).setValue('▲ BUY').setFontWeight('bold').setBackground(COLORS.BUY_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(11);
  sheet.getRange(row, 2).setValue(buyTotal).setFontWeight('bold').setBackground(COLORS.BUY_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 3).setValue(longOpen).setFontWeight('bold').setBackground(COLORS.FILLED_BG).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 4).setValue(buyAvail).setFontWeight('bold').setBackground(COLORS.AVAIL_BG).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.setRowHeight(row, 30);
  row++;

  sheet.getRange(row, 1).setValue('▼ SELL').setFontWeight('bold').setBackground(COLORS.SELL_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(11);
  sheet.getRange(row, 2).setValue(sellTotal).setFontWeight('bold').setBackground(COLORS.SELL_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 3).setValue(shortOpen).setFontWeight('bold').setBackground(COLORS.FILLED_BG).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 4).setValue(sellAvail).setFontWeight('bold').setBackground(COLORS.AVAIL_BG).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.setRowHeight(row, 30);
  row++;

  return row;
}

function renderFilterCard(sheet, row, basket) {
  const meta   = BASKET_META[basket];
  const config = fetchFilterConfig(basket);
  if (!config) return row;

  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`${meta.emoji}  ${meta.label.toUpperCase()}   ·   Target: ${config.target || 'S1'}   ·   Win%: ${config.win_pct || '—'}`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(11).setFontWeight('bold').setHorizontalAlignment('left');
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


// ════════════════════════════════════════════════════════════════════
//   TABS: BASKET FUNNELS
// ════════════════════════════════════════════════════════════════════

function refreshBasketFunnel(basket, rawDataArg) {
  const sheetName = {
    buy_reversal:  SHEETS.BR, buy_momentum: SHEETS.BM,
    sell_reversal: SHEETS.SR, sell_momentum: SHEETS.SM,
  }[basket];
  const sheet   = getOrCreate(sheetName);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const meta    = BASKET_META[basket];
  const config  = fetchFilterConfig(basket);
  const rawData = rawDataArg || getRawMetricsCached();
  const funnel  = fetchFunnelCounts(basket);

  if (!config || !rawData) {
    sheet.getRange(1, 1).setValue('⚠ API unreachable — retry refresh');
    return;
  }

  const filters      = config.filters;
  const ncol         = Math.min(filters.length, 13);
  const allStocks    = rawData.stocks || [];
  const colsPerFilter = 2;
  const totalCols    = 1 + ncol * colsPerFilter;
  const funnelCounts = funnel ? funnel.counts || {} : {};

  const stages = [];
  let survivors = allStocks.slice();
  filters.slice(0, ncol).forEach(f => {
    survivors = survivors.filter(s => {
      const v = s[f.metric];
      if (v === null || v === undefined || v === '') return false;
      if (f.min !== null && f.min !== undefined && Number(v) < Number(f.min)) return false;
      if (f.max !== null && f.max !== undefined && Number(v) > Number(f.max)) return false;
      return true;
    });
    stages.push(survivors.slice());
  });

  let row = 1;
  sheet.getRange(row, 1, 1, totalCols).merge()
    .setValue(`${meta.emoji}  ${meta.label.toUpperCase()} — Funnel Waterfall`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 34);
  row++;

  sheet.getRange(row, 1, 1, totalCols).merge()
    .setValue(`Universe: ${allStocks.length} F&O · Target: ${config.target || 'S1'} · Win%: ${config.win_pct || '—'} · each filter = [Stock | Value] · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row++;

  const symCol = i => 2 + i * colsPerFilter;
  const valCol = i => 3 + i * colsPerFilter;

  sheet.getRange(row, 1).setValue('Filter').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('center');
  filters.slice(0, ncol).forEach((f, i) => {
    sheet.getRange(row, symCol(i), 1, colsPerFilter).merge()
      .setValue(f.metric).setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
      .setHorizontalAlignment('center').setWrap(true);
  });
  sheet.setRowHeight(row, 36);
  row++;

  ['Min', 'Max'].forEach(label => {
    sheet.getRange(row, 1).setValue(label).setFontStyle('italic').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('center');
    filters.slice(0, ncol).forEach((f, i) => {
      const val = label === 'Min' ? f.min_display : f.max_display;
      sheet.getRange(row, symCol(i)).setValue('min →').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontStyle('italic').setFontSize(8).setHorizontalAlignment('right');
      sheet.getRange(row, valCol(i)).setValue(val === '' || val === undefined ? '—' : val)
        .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
    });
    row++;
  });

  sheet.getRange(row, 1).setValue('Count').setFontWeight('bold').setBackground(meta.color).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(12);
  filters.slice(0, ncol).forEach((f, i) => {
    const cnt = funnelCounts[f.metric] !== undefined ? funnelCounts[f.metric] : stages[i].length;
    sheet.getRange(row, symCol(i), 1, colsPerFilter).merge()
      .setValue(cnt).setFontWeight('bold').setBackground(meta.color).setFontColor(COLORS.WHITE)
      .setHorizontalAlignment('center').setFontSize(13).setFontFamily(FONTS.MONO.family);
  });
  sheet.setRowHeight(row, 32);
  row++;

  sheet.getRange(row, 1, 1, totalCols).merge()
    .setValue('▼  STOCKS PASSING EACH STAGE  (cumulative left → right · each filter shows the stock + its value for that metric)')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 20);
  row++;

  const maxLen = stages.reduce((m, s) => Math.max(m, s.length), 0);
  if (maxLen === 0) {
    sheet.getRange(row, 1, 1, totalCols).merge()
      .setValue('No stocks passed any filter today')
      .setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
  } else {
    sheet.getRange(row, 1).setValue('#').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setFontSize(9);
    stages.forEach((st, i) => {
      sheet.getRange(row, symCol(i)).setValue(`F${i + 1} (${st.length})`).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('left').setFontSize(9);
      sheet.getRange(row, valCol(i)).setValue('value').setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setFontSize(8).setFontStyle('italic');
    });
    row++;

    for (let r = 0; r < maxLen; r++) {
      const rowBg = (r % 2 === 0) ? COLORS.ALT_ROW : COLORS.CARD_BG;
      sheet.getRange(row, 1).setValue(r + 1).setFontFamily(FONTS.MONO.family).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setFontSize(9).setBackground(rowBg);
      stages.forEach((stageStocks, i) => {
        const metric  = filters[i].metric;
        const isFinal = (i === stages.length - 1);
        if (r < stageStocks.length) {
          const stock = stageStocks[r];
          const sym   = String(stock.symbol).replace('NSE:', '').replace('-EQ', '');
          sheet.getRange(row, symCol(i)).setValue(sym).setFontFamily(FONTS.HEADER.family)
            .setFontWeight(isFinal ? 'bold' : 'normal').setFontColor(isFinal ? meta.color : COLORS.NEUTRAL_TEXT)
            .setHorizontalAlignment('left').setBackground(rowBg);
          sheet.getRange(row, valCol(i)).setValue(fmtNum(stock[metric], 2))
            .setFontFamily(FONTS.MONO.family).setFontSize(9).setFontColor(COLORS.NEUTRAL_TEXT)
            .setHorizontalAlignment('right').setBackground(rowBg);
        } else {
          sheet.getRange(row, symCol(i)).setValue('').setBackground(rowBg);
          sheet.getRange(row, valCol(i)).setValue('').setBackground(rowBg);
        }
      });
      sheet.getRange(row, 1, 1, totalCols).setBorder(false, false, true, false, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    }
  }

  sheet.setColumnWidth(1, 36);
  filters.slice(0, ncol).forEach((f, i) => { sheet.setColumnWidth(symCol(i), 105); sheet.setColumnWidth(valCol(i), 58); });
  sheet.setFrozenRows(3);
  sheet.setFrozenColumns(1);
  toast(`✓ ${meta.label} refreshed`);
}


// ════════════════════════════════════════════════════════════════════
//   TAB: SELL OVERBOUGHT
// ════════════════════════════════════════════════════════════════════

function refreshSellOverbought() {
  const sheet  = getOrCreate(SHEETS.SO);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const data   = fetchSellOverbought();
  const config = fetchFilterConfig('sell_overbought');
  const meta   = BASKET_META.sell_overbought;
  let row = 1;

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`${meta.emoji}  SELL OVERBOUGHT — Failed Breakout / Exhaustion Reversal`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  if (!data) { sheet.getRange(row, 1).setValue('⚠ API unreachable'); return; }

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`Target: ${data.target || 'S1'} · SL: ${data.sl || '1:1'} · Backtest May-26: ${data.win_pct_may2026 || '71.4%'} · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row++;

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue('ℹ ' + (data.note || 'Market gate required — fails in recovery/bull markets'))
    .setBackground('#FEF3C7').setFontColor('#92400E').setFontStyle('italic').setFontSize(10);
  sheet.setRowHeight(row, 24);
  row += 2;

  row = renderSubHeader(sheet, row, 'FILTER LOGIC');
  row = renderTableHeader(sheet, row, ['Filter', 'Min', 'Max', 'Logic'], 4);

  if (config && config.filters && config.filters.length > 0) {
    config.filters.forEach(f => {
      sheet.getRange(row, 1).setValue(f.metric).setFontWeight('bold').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 2).setValue(f.min_display !== '' && f.min_display !== null ? f.min_display : '—').setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 3).setValue(f.max_display !== '' && f.max_display !== null ? f.max_display : '—').setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 4).setValue(humanLogic(f.metric)).setFontColor(COLORS.NEUTRAL_TEXT).setFontStyle('italic').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 1, 1, 4).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  } else {
    [['dma_200','≥ 10%','—','Extended above 200-day MA'],
     ['week_index_52','≥ 80','—','Near 52-week high'],
     ['rsi_month','≥ 60','—','RSI elevated'],
     ['day_change','—','< 0','Previous close red (2-day net %)'],
    ].forEach(f => {
      sheet.getRange(row, 1).setValue(f[0]).setFontWeight('bold').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 2).setValue(f[1]).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 3).setValue(f[2]).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 4).setValue(f[3]).setFontColor(COLORS.NEUTRAL_TEXT).setFontStyle('italic').setBackground(COLORS.CARD_BG);
      sheet.getRange(row, 1, 1, 4).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
    sheet.getRange(row, 1, 1, 4).merge().setValue('⚠ filter_config unavailable — showing cached thresholds')
      .setBackground('#FEF3C7').setFontColor('#92400E').setFontStyle('italic').setFontSize(9);
    row++;
  }
  row += 2;

  row = renderSubHeader(sheet, row, `LIVE SIGNALS (${data.count || 0} qualified)`);
  const headers = ['Symbol', 'Entry', 'Target (S1)', 'Stop', 'Tgt %', 'DMA200', 'wi52', 'ma9_21', 'Vol Ratio', '2D Chg%', 'RSI M'];
  row = renderTableHeader(sheet, row, headers, headers.length);

  const stocks = data.stocks || [];
  if (stocks.length === 0) {
    sheet.getRange(row, 1, 1, headers.length).merge()
      .setValue('No qualified Sell Overbought signals today')
      .setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
  } else {
    stocks.forEach(s => {
      const vals = [
        s.symbol, fmtNum(s.entry, 2), fmtNum(s.target, 2), fmtNum(s.stop, 2),
        fmtPct(s.tgt_pct), fmtPct(s.dma_200), fmtNum(s.week_index_52, 1),
        fmtPct(s.ma9_vs_ma21), fmtNum(s.vol_ratio, 2), fmtPct(s.day_change), fmtNum(s.rsi_month, 1),
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
  sheet.setColumnWidths(2, headers.length - 1, 95);
  sheet.setFrozenRows(2);
  toast('✓ Sell Overbought refreshed');
}


// ════════════════════════════════════════════════════════════════════
//   TAB: IN POSITION
// ════════════════════════════════════════════════════════════════════

function refreshInPosition() {
  const sheet     = getOrCreate(SHEETS.POS);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const posData   = fetchPositions() || {};
  const positions = posData.open_positions || [];
  const mood      = fetchMarketMood();

  let row = 1;
  sheet.getRange(row, 1, 1, 11).merge()
    .setValue('📍  IN POSITION — PAPER ENGINE (Live Open Positions)')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  sheet.getRange(row, 1, 1, 11).merge()
    .setValue(mood
      ? `Gate: ${mood.fails === 0 ? '✅ OPEN' : '❌ CLOSED'}   |   Buy: ${mood.buy_slots}   |   Sell: ${mood.sell_slots}   |   Refreshed: ${nowIST()}`
      : `Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(10);
  sheet.setRowHeight(row, 22);
  row++;

  sheet.getRange(row, 1, 1, 11).merge()
    .setValue('ℹ CMP + unrealised P&L computed server-side.')
    .setBackground('#FEF3C7').setFontColor('#92400E').setFontSize(8).setFontStyle('italic');
  sheet.setRowHeight(row, 24);
  row += 2;

  BASKETS.forEach(basket => {
    const meta   = BASKET_META[basket];
    const trades = positions.filter(p => normalizeBasket(p.basket || p.v8_basket || '') === meta.label);
    row = renderPositionSection(sheet, row, meta, trades);
    row += 1;
  });

  row = renderSectionHeader(sheet, row, '📊  OVERALL OPEN SUMMARY', COLORS.DARK_HEADER);
  const totalPnl = positions.reduce((s, p) => s + Number(p.unrealised_pnl || 0), 0);
  const winning  = positions.filter(p => Number(p.unrealised_pnl || 0) > 0).length;
  const losing   = positions.filter(p => Number(p.unrealised_pnl || 0) < 0).length;
  [['Total Open', positions.length, 'Winning', winning, 'Losing', losing, 'Total P&L', fmtPnL(totalPnl)]].forEach(srow => {
    for (let i = 0; i < srow.length; i++) {
      const cell = sheet.getRange(row, 1 + i);
      if (i % 2 === 0) cell.setValue(srow[i]).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
      else {
        cell.setValue(srow[i]).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center');
        if (i === 7) cell.setFontColor(totalPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
      }
    }
    row++;
  });

  sheet.setColumnWidth(1, 130);
  sheet.setColumnWidths(2, 10, 110);
  sheet.setFrozenRows(4);
  toast('✓ In Position refreshed');
}

function renderPositionSection(sheet, row, meta, trades) {
  sheet.getRange(row, 1, 1, 11).merge()
    .setValue(`${meta.emoji}  ${meta.label.toUpperCase()}`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(11).setFontWeight('bold');
  sheet.setRowHeight(row, 26);
  row++;

  const totalPnl = trades.reduce((s, t) => s + Number(t.unrealised_pnl || 0), 0);
  const winning  = trades.filter(t => Number(t.unrealised_pnl || 0) > 0).length;
  const losing   = trades.filter(t => Number(t.unrealised_pnl || 0) < 0).length;
  const avgPnl   = trades.length > 0 ? totalPnl / trades.length : 0;
  const pctGreen = trades.length > 0 ? (winning / trades.length * 100).toFixed(2) + '%' : '0.00%';

  ['Open', 'Unrealised P&L', '% Green', 'In Profit', 'In Loss', 'Avg P&L/Trade'].forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG)
      .setFontSize(9).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center');
  });
  row++;

  [trades.length, fmtPnL(totalPnl), pctGreen, winning, losing, fmtPnL(avgPnl)].forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center').setFontSize(11);
    if (i === 1) cell.setFontColor(totalPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
    if (i === 5) cell.setFontColor(avgPnl   >= 0 ? COLORS.PROFIT : COLORS.LOSS);
  });
  sheet.setRowHeight(row, 24);
  row++;

  const tradeHeaders = ['Entry Time', 'Symbol', 'Side', 'Entry', 'CMP', 'Qty', 'SL', 'Target', 'Unrealised P&L', 'Holding', 'Pivot Date'];
  row = renderTableHeader(sheet, row, tradeHeaders, tradeHeaders.length);

  if (trades.length === 0) {
    sheet.getRange(row, 1, 1, tradeHeaders.length).merge()
      .setValue('No open paper positions').setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT)
      .setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
    row++;
  } else {
    trades.forEach(t => {
      const pnl  = Number(t.unrealised_pnl || 0);
      const vals = [
        fmtDate(t.entry_ts), t.symbol,
        t.side || (t.basket && t.basket.startsWith('buy') ? 'LONG' : 'SHORT'),
        fmtNum(t.entry_price, 2), fmtNum(t.cmp || t.entry_price, 2),
        t.qty || 1, fmtNum(t.stop_loss, 2), fmtNum(t.target, 2),
        fmtPnL(pnl), computeHolding(t.entry_ts), t.pivot_date || '—',
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v).setFontFamily(i === 1 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 1 ? 'bold' : 'normal').setHorizontalAlignment(i === 1 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
        if (i === 8) cell.setFontColor(pnl >= 0 ? COLORS.PROFIT : COLORS.LOSS).setFontWeight('bold');
      });
      sheet.getRange(row, 1, 1, tradeHeaders.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }
  return row;
}


// ════════════════════════════════════════════════════════════════════
//   TAB: TRADE LOG
// ════════════════════════════════════════════════════════════════════

function refreshTradeLog() {
  const sheet   = getOrCreate(SHEETS.LOG);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const posData = fetchPositions() || {};
  const trades  = posData.recent_trades || [];

  let row = 1;
  sheet.getRange(row, 1, 1, 11).merge()
    .setValue('📒  TRADE LOG — CLOSED TRADES (Paper Engine)')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;
  sheet.getRange(row, 1, 1, 11).merge()
    .setValue(`Last refresh: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const wins     = trades.filter(t => t.result === 'TARGET').length;
  const losses   = trades.filter(t => t.result === 'SL').length;
  const gateEx   = trades.filter(t => t.result === 'GATE_EXIT').length;
  const totalPnl = trades.reduce((s, t) => s + Number(t.pnl || 0), 0);
  const accuracy = trades.length > 0 ? (wins / trades.length * 100).toFixed(1) + '%' : '—';

  row = renderSubHeader(sheet, row, '📊  OVERALL SUMMARY');
  row = renderTableHeader(sheet, row, ['Total Trades', 'Total P&L', 'Accuracy', 'Target Hit', 'SL Hit', 'Gate Exit'], 6);
  [trades.length, fmtPnL(totalPnl), accuracy, wins, losses, gateEx].forEach((v, i) => {
    const cell = sheet.getRange(row, 1 + i);
    cell.setValue(v).setFontFamily(FONTS.MONO.family).setFontWeight('bold').setHorizontalAlignment('center').setFontSize(11);
    if (i === 1) cell.setFontColor(totalPnl >= 0 ? COLORS.PROFIT : COLORS.LOSS);
  });
  row += 2;

  const tradeHeaders = ['Entry Time', 'Exit Time', 'Symbol', 'Side', 'Basket', 'Entry', 'Exit', 'Qty', 'P&L', 'Ret%', 'Result'];
  row = renderTableHeader(sheet, row, tradeHeaders, tradeHeaders.length);

  if (trades.length === 0) {
    sheet.getRange(row, 1, 1, tradeHeaders.length).merge()
      .setValue('No closed trades yet').setFontStyle('italic').setFontColor(COLORS.NEUTRAL_TEXT)
      .setHorizontalAlignment('center').setBackground(COLORS.NEUTRAL_BG);
    row++;
  } else {
    trades.forEach(t => {
      const pnl   = Number(t.pnl || 0);
      const isWin = t.result === 'TARGET';
      const vals  = [
        fmtDate(t.entry_ts), fmtDate(t.exit_ts), t.symbol, t.side,
        t.basket || '—', fmtNum(t.entry_price, 2), fmtNum(t.exit_price, 2),
        t.qty || 1, fmtPnL(pnl), fmtPct(t.return_pct), t.result || '—',
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v).setFontFamily(i === 2 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 2 ? 'bold' : 'normal').setHorizontalAlignment(i === 2 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
        if (i === 8) cell.setFontColor(pnl >= 0 ? COLORS.PROFIT : COLORS.LOSS).setFontWeight('bold');
        if (i === 10) {
          cell.setFontColor(isWin ? COLORS.PASS_TEXT : COLORS.FAIL_TEXT)
            .setBackground(isWin ? COLORS.PASS_BG : COLORS.FAIL_BG)
            .setFontWeight('bold').setHorizontalAlignment('center');
        }
      });
      sheet.getRange(row, 1, 1, tradeHeaders.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }
  sheet.setColumnWidth(1, 130);
  sheet.setColumnWidths(2, tradeHeaders.length - 1, 100);
  sheet.setFrozenRows(2);
  toast('✓ Trade Log refreshed');
}


// ════════════════════════════════════════════════════════════════════
//   TAB: RAW DATA — 21 cols: + sector_week, sector_month, PP, R1, R2, S1, S2
// ════════════════════════════════════════════════════════════════════

function refreshRawData() {
  const sheet = getOrCreate(SHEETS.RAW);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const data = fetchRawMetrics();
  let row = 1;
  sheet.getRange(row, 1, 1, 21).merge()
    .setValue('📋  RAW DATA — All Futures Metrics (GVM-sorted)')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  if (!data) { sheet.getRange(row, 1).setValue('⚠ API unreachable'); return; }

  sheet.getRange(row, 1, 1, 21).merge()
    .setValue(`Score date: ${data.score_date || '—'} · ${data.count || 0} stocks · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const headers = [
    'Symbol', 'GVM', 'DMA20', 'DMA50', 'DMA200',
    'RSI M', 'RSI W', 'RSI D',
    'M Ret%', 'W Ret%', '2D Chg%', 'Y Ret%',
    'Mth Idx', 'wi52',
    'Sec W%', 'Sec M%',
    'PP', 'R1', 'R2', 'S1', 'S2',
  ];
  row = renderTableHeader(sheet, row, headers, headers.length);

  (data.stocks || []).forEach(s => {
    const vals = [
      s.symbol,
      fmtNum(s.gvm_score, 2),
      fmtNum(s.dma_20, 2),
      fmtNum(s.dma_50, 2),
      fmtNum(s.dma_200, 2),
      fmtNum(s.rsi_month, 1),
      fmtNum(s.rsi_weekly, 1),
      fmtNum(s.daily_rsi, 1),
      fmtNum(s.month_return, 2),
      fmtNum(s.week_return, 2),
      fmtNum(s.day_change, 2),
      fmtNum(s.year_return, 2),
      fmtNum(s.month_index, 1),
      fmtNum(s.week_index_52, 1),
      fmtNum(s.sector_week, 2),
      fmtNum(s.sector_month, 2),
      fmtNum(s.pp, 2),
      fmtNum(s.r1, 2),
      fmtNum(s.r2, 2),
      fmtNum(s.s1, 2),
      fmtNum(s.s2, 2),
    ];
    vals.forEach((v, i) => {
      sheet.getRange(row, 1 + i).setValue(v)
        .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
        .setFontWeight(i === 0 ? 'bold' : 'normal')
        .setHorizontalAlignment(i === 0 ? 'left' : 'right')
        .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
    });
    sheet.getRange(row, 1, 1, headers.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
    row++;
  });

  sheet.setColumnWidth(1, 130);
  sheet.setColumnWidths(2, headers.length - 1, 72);
  sheet.setFrozenRows(5);
  toast('✓ Raw Data refreshed');
}


// ════════════════════════════════════════════════════════════════════
//   TAB: FILTER SCAN
// ════════════════════════════════════════════════════════════════════

function refreshFilterScan() {
  const sheet = getOrCreate(SHEETS.SCAN);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  let row = 1;
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue('🔍  FILTER SCAN — All 5 Basket Configs')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;
  sheet.getRange(row, 1, 1, 10).merge()
    .setValue(`2D gate: day_change (2-day net close-to-close%) · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  BASKETS.forEach(basket => {
    const meta   = BASKET_META[basket];
    const config = fetchFilterConfig(basket);
    if (!config) return;

    sheet.getRange(row, 1, 1, 10).merge()
      .setValue(`${meta.emoji}  ${meta.label.toUpperCase()}   ·   Side: ${config.side || '—'}   ·   Target: ${config.target || 'S1'}   ·   Win%: ${config.win_pct || '—'}`)
      .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(11).setFontWeight('bold');
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
    row += 2;
  });

  sheet.setColumnWidth(1, 160);
  sheet.setColumnWidths(2, 3, 90);
  sheet.setColumnWidth(4, 200);
  sheet.setFrozenRows(4);
  toast('✓ Filter Scan refreshed');
}
