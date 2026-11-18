/* Standalone layout calculation for the future gallery; no DOM or dependencies. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.JoneGalleryGrid = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  function calculate(count, width, height, options) {
    options = options || {};
    var gap = options.gap == null ? 16 : options.gap;
    var minWidth = options.minWidth == null ? 200 : options.minWidth;
    var minHeight = options.minHeight == null ? 150 : options.minHeight;
    [count, width, height, gap, minWidth, minHeight].forEach(function (n) {
      if (typeof n !== 'number' || !isFinite(n)) throw new TypeError('Expected finite numeric inputs');
    });
    if (count < 0 || Math.floor(count) !== count || width <= 0 || height <= 0 || gap < 0 || minWidth <= 0 || minHeight <= 0) throw new RangeError('Invalid grid input');
    if (count === 0) return {columns: 0, rows: 0, pageSize: 0, pages: 0, cardWidth: 0, cardHeight: 0, belowMinimum: false};
    var maxCols = Math.max(1, Math.min(count, Math.floor((width + gap) / (minWidth + gap))));
    var maxRows = Math.max(1, Math.min(count, Math.floor((height + gap) / (minHeight + gap))));
    var best = null;
    for (var cols = 1; cols <= maxCols; cols++) {
      for (var rows = 1; rows <= maxRows; rows++) {
        if ((cols - 1) * rows >= count || (rows - 1) * cols >= count) continue;
        var cellWidth = (width - (cols - 1) * gap) / cols;
        var cellHeight = (height - (rows - 1) * gap) / rows;
        if (cellWidth <= 0 || cellHeight <= 0) continue;
        // Reserve room for the number and keep the image landscape, rather than
        // cropping a wide room photograph into a towering portrait card.
        var labelHeight = Math.min(40, cellHeight * 0.25);
        var w = Math.min(cellWidth, (cellHeight - labelHeight) * 1.5);
        var h = Math.min(cellHeight, w / 1.5 + labelHeight);
        if ((w < minWidth || h < minHeight) && (width >= minWidth && height >= minHeight)) continue;
        var pageSize = cols * rows;
        var pages = Math.ceil(count / pageSize);
        // Fit one page first, then use larger cards; favor landscape cards on ties.
        var area = w * h;
        var shapePenalty = Math.abs(Math.log((w / h) / 1.5));
        if (!best || pages < best.pages || (pages === best.pages && (area > best.area + 0.01 || (Math.abs(area - best.area) < 0.01 && shapePenalty < best.shapePenalty)))) {
          best = {columns: cols, rows: rows, pageSize: pageSize, pages: pages, cardWidth: w, cardHeight: h, belowMinimum: w < minWidth || h < minHeight, area: area, shapePenalty: shapePenalty};
        }
      }
    }
    if (!best) { best = {columns:1, rows:1, pageSize:1, pages:count, cardWidth:width, cardHeight:height, belowMinimum:true}; }
    delete best.area; delete best.shapePenalty;
    return best;
  }
  return {calculate: calculate};
}));
