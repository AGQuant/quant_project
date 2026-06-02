/**
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║   SCORR V8 — Tabs: All Sheet Renderers + Aggregations           ║
 * ║   Part 2 of 2  →  v8_dashboard_tabs.gs                          ║
 * ║   Requires    →  v8_dashboard_core.gs (config + helpers)        ║
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
  BASKETS.forEach(basket => {
    row = renderFilterCard(sheet, row, basket);
    row += 1;
  });

  sheet.setFrozenRows(2);
  toast('✓ Dashboard refreshed');
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

function renderPerformanceSummary(sheet, row) {
  const posData   = fetchPositions() || {};
  const positions = posData.open_positions || [];
  const trades    = posData.recent_trades  || [];

  const openAgg   = aggregatePositions(positions);
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
      strat, dir, a.count, a.pnl, fmtPct(a.accuracy), a.targetHit, a.slGap, a.avgPnl,
    ], { pnlCols: [4, 8] });
  });
  const totC = closedAgg.__TOTAL || zeroAgg();
  row = renderTotalRow(sheet, row, 8, ['TOTAL', 'ALL', totC.count, totC.pnl, fmtPct(totC.accuracy), totC.targetHit, totC.slGap, totC.avgPnl], { pnlCols: [4, 8] });

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

  sheet.getRange(row, 1, 1, 2).merge().setValue(gateText).setBackground(gateBg).setFontColor(gateFg).setFontSize(14).setFontWeight('bold').setHorizontalAlignment('center');
  sheet.getRange(row, 3).setValue(`${mood.fails} filters failing`).setFontStyle('italic').setHorizontalAlignment('center');
  sheet.getRange(row, 4).setValue('Mood:').setFontWeight('bold').setHorizontalAlignment('right');
  sheet.getRange(row, 5).setValue(mood.mood).setFontWeight('bold').setHorizontalAlignment('left');
  sheet.getRange(row, 6).setValue('Buy:').setFontWeight('bold').setHorizontalAlignment('right');
  sheet.getRange(row, 7).setValue(mood.buy_slots).setFontWeight('bold').setBackground(COLORS.BUY_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 8).setValue('Sell:').setFontWeight('bold').setHorizontalAlignment('right');
  sheet.getRange(row, 9).setValue(mood.sell_slots).setFontWeight('bold').setBackground(COLORS.SELL_REV).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setFontSize(14);
  sheet.getRange(row, 10).setValue('/15').setFontColor(COLORS.NEUTRAL_TEXT).setFontStyle('italic');
  sheet.setRowHeight(row, 32);
  return row + 1;
}

function renderFilterCard(sheet, row, basket) {
  const meta   = BASKET_META[basket];
  const config = fetchFilterConfig(basket);
  if (!config) return row;

  const header = sheet.getRange(row, 1, 1, 10).merge();
  header.setValue(`${meta.emoji}  ${meta.label.toUpperCase()}   ·   Target: ${config.target || 'S1'}   ·   Win%: ${config.win_pct || '—'}`)
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

function refreshBasketFunnel(basket) {
  const sheetName = {
    buy_reversal:  SHEETS.BR, buy_momentum: SHEETS.BM,
    sell_reversal: SHEETS.SR, sell_momentum: SHEETS.SM,
  }[basket];
  const sheet = getOrCreate(sheetName);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const meta      = BASKET_META[basket];
  const config    = fetchFilterConfig(basket);
  const qualified = fetchQualified(basket);

  if (!config || !qualified) {
    sheet.getRange(1, 1).setValue('⚠ API unreachable');
    return;
  }

  let row = 1;
  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`${meta.emoji}  ${meta.label.toUpperCase()} — Stock Funnel`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE)
    .setFontSize(15).setFontWeight('bold').setHorizontalAlignment('left');
  sheet.setRowHeight(row, 34);
  row++;

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`Universe: 210 F&O · Target: ${config.target || 'S1'} · Win%: ${config.win_pct || '—'} · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT)
    .setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const filters = config.filters;
  const ncol    = Math.min(filters.length, 12);

  sheet.getRange(row, 1).setValue('Filter').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE);
  filters.slice(0, ncol).forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.metric).setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('center').setWrap(true);
  });
  sheet.setRowHeight(row, 36);
  row++;

  sheet.getRange(row, 1).setValue('Min').setFontStyle('italic').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  filters.slice(0, ncol).forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.min_display).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  });
  row++;

  sheet.getRange(row, 1).setValue('Max').setFontStyle('italic').setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  filters.slice(0, ncol).forEach((f, i) => {
    sheet.getRange(row, 2 + i).setValue(f.max_display).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.WHITE).setFontFamily(FONTS.MONO.family).setHorizontalAlignment('center');
  });
  row += 2;

  const qcount = qualified.count || 0;
  sheet.getRange(row, 1).setValue('QUALIFIED').setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setHorizontalAlignment('right');
  sheet.getRange(row, 2, 1, ncol).merge()
    .setValue(`${qcount} stocks passed all filters`)
    .setFontWeight('bold').setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE)
    .setHorizontalAlignment('center').setFontSize(12);
  sheet.setRowHeight(row, 28);
  row += 2;

  const stockHeaders = ['Symbol', 'GVM', 'DMA200', 'DMA50', 'RSI M', 'RSI W', 'M Ret%', 'W Ret%', '1D Net%', 'Sec Wk', 'Sec Day'];
  row = renderTableHeader(sheet, row, stockHeaders, stockHeaders.length);

  const stocks = qualified.stocks || [];
  if (stocks.length === 0) {
    sheet.getRange(row, 1, 1, stockHeaders.length).merge()
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
        fmtNum(s.prev_day_change, 2),
        fmtNum(s.sector_week, 2),
        fmtNum(s.sector_day, 2),
      ];
      vals.forEach((v, i) => {
        sheet.getRange(row, 1 + i).setValue(v)
          .setFontFamily(i === 0 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 0 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 0 ? 'left' : 'right')
          .setBackground(row % 2 === 0 ? COLORS.ALT_ROW : COLORS.CARD_BG);
      });
      sheet.getRange(row, 1, 1, stockHeaders.length).setBorder(true, true, true, true, false, false, COLORS.BORDER_SOFT, SpreadsheetApp.BorderStyle.SOLID);
      row++;
    });
  }

  sheet.setColumnWidth(1, 140);
  sheet.setColumnWidths(2, ncol, 85);
  sheet.setFrozenRows(3);
  toast(`✓ ${meta.label} refreshed`);
}


// ════════════════════════════════════════════════════════════════════
//   TAB: SELL OVERBOUGHT
// ════════════════════════════════════════════════════════════════════

function refreshSellOverbought() {
  const sheet = getOrCreate(SHEETS.SO);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const data = fetchSellOverbought();
  const meta = BASKET_META.sell_overbought;
  let row = 1;

  sheet.getRange(row, 1, 1, 14).merge()
    .setValue(`${meta.emoji}  SELL OVERBOUGHT — Failed Breakout / Exhaustion Reversal`)
    .setBackground(meta.color).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  if (!data) {
    sheet.getRange(row, 1).setValue('⚠ API unreachable');
    return;
  }

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
  const filterRows = [
    ['DMA 200',       '≥ 10%',  'Extended above 200-day MA'],
    ['52-week index', '≥ 80',   'Near 52-week high'],
    ['MA9 vs MA21',   '≥ 3%',   'Short-term momentum stretched'],
    ['Volume ratio',  '≤ 0.8',  'Volume drying — distribution starting'],
    ['1D net return', '< 0%',   'Closed red (prev_day_change < 0)'],
    ['RSI Month',     '≥ 60',   'RSI elevated'],
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
  const headers = ['Symbol', 'Entry', 'Target (S1)', 'Stop', 'Tgt %', 'DMA200', 'wi52', 'ma9_21', 'Vol Ratio', '1D Net%', 'RSI M'];
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
        s.symbol,
        fmtNum(s.entry, 2),
        fmtNum(s.target, 2),
        fmtNum(s.stop, 2),
        fmtPct(s.tgt_pct),
        fmtPct(s.dma_200),
        fmtNum(s.week_index_52, 1),
        fmtPct(s.ma9_vs_ma21),
        fmtNum(s.vol_ratio, 2),
        fmtPct(s.prev_day_change),
        fmtNum(s.rsi_month, 1),
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
  const sheet = getOrCreate(SHEETS.POS);
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

  const gateText = mood
    ? `Gate: ${mood.fails === 0 ? '✅ OPEN' : '❌ CLOSED'}   |   Buy: ${mood.buy_slots}   |   Sell: ${mood.sell_slots}   |   Max: 15   |   Refreshed: ${nowIST()}`
    : `Refreshed: ${nowIST()}`;
  sheet.getRange(row, 1, 1, 11).merge()
    .setValue(gateText).setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(10);
  sheet.setRowHeight(row, 22);
  row++;

  sheet.getRange(row, 1, 1, 11).merge()
    .setValue('ℹ What the engine ACTUALLY entered (sticky until exit) — won\'t match Today\'s Signals on the dashboard, which are live qualifiers. % Green = share of open trades currently in profit, NOT a closed-trade win rate.')
    .setBackground('#FEF3C7').setFontColor('#92400E').setFontSize(8).setFontStyle('italic').setWrap(true);
  sheet.setRowHeight(row, 30);
  row += 2;

  const BASKET_ORDER = ['buy_reversal', 'buy_momentum', 'sell_reversal', 'sell_momentum', 'sell_overbought'];
  BASKET_ORDER.forEach(basket => {
    const meta   = BASKET_META[basket];
    const trades = positions.filter(p => (p.basket || p.v8_basket || '').toLowerCase() === basket);
    row = renderPositionSection(sheet, row, meta, trades);
    row += 1;
  });

  row = renderSectionHeader(sheet, row, '📊  OVERALL OPEN SUMMARY', COLORS.DARK_HEADER);
  const totalPnl = positions.reduce((s, p) => s + Number(p.unrealised_pnl || 0), 0);
  const winning  = positions.filter(p => Number(p.unrealised_pnl || 0) > 0).length;
  const losing   = positions.filter(p => Number(p.unrealised_pnl || 0) < 0).length;

  const summaryData = [['Total Open', positions.length, 'Winning', winning, 'Losing', losing, 'Total P&L', fmtPnL(totalPnl)]];
  summaryData.forEach(srow => {
    for (let i = 0; i < srow.length; i++) {
      const cell = sheet.getRange(row, 1 + i);
      if (i % 2 === 0) {
        cell.setValue(srow[i]).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG).setHorizontalAlignment('right');
      } else {
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
  const pctGreen = trades.length > 0 ? (winning / trades.length * 100).toFixed(2) + '%' : '0.00%';
  const avgPnl   = trades.length > 0 ? totalPnl / trades.length : 0;

  const statHeaders = ['Open', 'Unrealised P&L', '% Green', 'In Profit', 'In Loss', 'Avg P&L/Trade'];
  statHeaders.forEach((h, i) => {
    sheet.getRange(row, 1 + i).setValue(h).setFontWeight('bold').setBackground(COLORS.NEUTRAL_BG)
      .setFontSize(9).setFontColor(COLORS.NEUTRAL_TEXT).setHorizontalAlignment('center');
  });
  row++;

  const statVals = [trades.length, fmtPnL(totalPnl), pctGreen, winning, losing, fmtPnL(avgPnl)];
  statVals.forEach((v, i) => {
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
        fmtDate(t.entry_ts),
        t.symbol,
        t.side || (t.basket && t.basket.startsWith('buy') ? 'LONG' : 'SHORT'),
        fmtNum(t.entry_price, 2),
        fmtNum(t.cmp || t.entry_price, 2),
        t.qty || 1,
        fmtNum(t.stop_loss, 2),
        fmtNum(t.target, 2),
        fmtPnL(pnl),
        computeHolding(t.entry_ts),
        t.pivot_date || '—',
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v)
          .setFontFamily(i === 1 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 1 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 1 ? 'left' : 'right')
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
  const sheet = getOrCreate(SHEETS.LOG);
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
  const sumHeaders = ['Total Trades', 'Total P&L', 'Accuracy', 'Target Hit', 'SL Hit', 'Gate Exit'];
  const sumVals    = [trades.length, fmtPnL(totalPnl), accuracy, wins, losses, gateEx];
  row = renderTableHeader(sheet, row, sumHeaders, 6);
  sumVals.forEach((v, i) => {
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
        fmtDate(t.entry_ts),
        fmtDate(t.exit_ts),
        t.symbol,
        t.side,
        t.basket || '—',
        fmtNum(t.entry_price, 2),
        fmtNum(t.exit_price, 2),
        t.qty || 1,
        fmtPnL(pnl),
        fmtPct(t.return_pct),
        t.result || '—',
      ];
      vals.forEach((v, i) => {
        const cell = sheet.getRange(row, 1 + i);
        cell.setValue(v)
          .setFontFamily(i === 2 ? FONTS.HEADER.family : FONTS.MONO.family)
          .setFontWeight(i === 2 ? 'bold' : 'normal')
          .setHorizontalAlignment(i === 2 ? 'left' : 'right')
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
//   TAB: RAW DATA
// ════════════════════════════════════════════════════════════════════

function refreshRawData() {
  const sheet = getOrCreate(SHEETS.RAW);
  sheet.clear().clearConditionalFormatRules();
  sheet.setHiddenGridlines(true);

  const data = fetchRawMetrics();
  let row = 1;

  sheet.getRange(row, 1, 1, 16).merge()
    .setValue('📋  RAW DATA — All Futures Metrics (GVM-sorted)')
    .setBackground(COLORS.DARK_HEADER).setFontColor(COLORS.WHITE).setFontSize(15).setFontWeight('bold');
  sheet.setRowHeight(row, 34);
  row++;

  if (!data) {
    sheet.getRange(row, 1).setValue('⚠ API unreachable');
    return;
  }

  sheet.getRange(row, 1, 1, 16).merge()
    .setValue(`Score date: ${data.score_date || '—'} · ${data.count || 0} stocks · Refreshed: ${nowIST()}`)
    .setBackground(COLORS.SUBHEADER).setFontColor(COLORS.MUTED_LIGHT).setFontSize(9).setFontStyle('italic');
  sheet.setRowHeight(row, 20);
  row += 2;

  const headers = ['Symbol', 'GVM', 'DMA20', 'DMA50', 'DMA200', 'RSI M', 'RSI W', 'RSI D', 'M Ret%', 'W Ret%', '1D Net%', 'Y Ret%', 'Sec Day', 'Sec Wk', 'Mth Idx', 'wi52'];
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
      fmtNum(s.prev_day_change, 2),
      fmtNum(s.year_return, 2),
      fmtNum(s.sector_day, 2),
      fmtNum(s.sector_week, 2),
      fmtNum(s.month_index, 1),
      fmtNum(s.week_index_52, 1),
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
  sheet.setColumnWidths(2, headers.length - 1, 75);
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
    .setValue(`1D gate: prev_day_change (net close-to-close%) · Refreshed: ${nowIST()}`)
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


// ════════════════════════════════════════════════════════════════════
//   AGGREGATION HELPERS
// ════════════════════════════════════════════════════════════════════

function aggregatePositions(positions) {
  const out = { __TOTAL: zeroAgg() };
  positions.forEach(p => {
    const strat = normalizeBasket(p.basket || p.v8_basket || '');
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl = Number(p.unrealised_pnl || 0);
    out[strat].count++;
    out[strat].pnl += pnl;
    if (pnl > 0) out[strat].winning++;
    else if (pnl < 0) out[strat].losing++;
    out.__TOTAL.count++;
    out.__TOTAL.pnl += pnl;
    if (pnl > 0) out.__TOTAL.winning++;
    else if (pnl < 0) out.__TOTAL.losing++;
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
  const out = { __TOTAL: zeroAgg() };
  trades.forEach(t => {
    const strat  = normalizeBasket(t.basket || '');
    if (!out[strat]) out[strat] = zeroAgg();
    const pnl    = Number(t.pnl || 0);
    const result = (t.result || '').toUpperCase();
    out[strat].count++;
    out[strat].pnl += pnl;
    if (result === 'TARGET') out[strat].targetHit++;
    else out[strat].slGap++;
    out.__TOTAL.count++;
    out.__TOTAL.pnl += pnl;
    if (result === 'TARGET') out.__TOTAL.targetHit++;
    else out.__TOTAL.slGap++;
  });
  Object.keys(out).forEach(k => {
    const a = out[k];
    a.accuracy = a.count > 0 ? (a.targetHit / a.count) * 100 : 0;
    a.avgPnl   = a.count > 0 ? a.pnl / a.count : 0;
    a.pnl      = fmtPnL(a.pnl);
    a.avgPnl   = fmtPnL(a.avgPnl);
  });
  return out;
}

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
