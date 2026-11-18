'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const grid = require('../rooms-gallery/grid.js');

test('empty gallery requires no pages', () => {
  assert.equal(grid.calculate(0, 1920, 900).pages, 0);
});
test('normal TV rooms fit on screen at legible minimum dimensions', () => {
  const x = grid.calculate(24, 1840, 780);
  assert.equal(x.pages, 1);
  assert.ok(x.pageSize >= 24);
  assert.ok(x.cardWidth >= 200 && x.cardHeight >= 150);
  assert.ok(x.columns * x.cardWidth + (x.columns - 1) * 16 <= 1840.01);
  assert.ok(x.rows * x.cardHeight + (x.rows - 1) * 16 <= 780.01);
});
test('large numbers paginate instead of shrinking below defaults', () => {
  const x = grid.calculate(100, 1840, 780);
  assert.ok(x.pages > 1);
  assert.ok(x.cardWidth >= 200 && x.cardHeight >= 150);
  assert.equal(x.pages, Math.ceil(100 / x.pageSize));
});
test('small window gracefully signals minimum cannot fit', () => {
  const x = grid.calculate(2, 130, 100);
  assert.equal(x.pageSize, 1);
  assert.equal(x.pages, 2);
  assert.equal(x.belowMinimum, true);
});
test('portrait layout and zero gap fit within the supplied region', () => {
  const x = grid.calculate(6, 360, 750, {minWidth: 140, minHeight: 150, gap: 0});
  assert.equal(x.pages, 1);
  assert.ok(x.columns * x.cardWidth <= 360.01);
  assert.ok(x.rows * x.cardHeight <= 750.01);
});
test('invalid inputs are refused', () => {
  for (const args of [[-1, 100, 100], [1, 0, 100], [1.5, 100, 100], [1, NaN, 100]]) {
    assert.throws(() => grid.calculate(...args));
  }
});
