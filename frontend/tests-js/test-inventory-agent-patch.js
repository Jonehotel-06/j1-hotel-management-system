const fs = require('fs');
const assert = require('assert');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../dashboard/inventory.html'), 'utf8');
for (const type of ['TRANSFER_IN', 'TRANSFER_OUT', 'POS_RETURN', 'MAINTENANCE_RETURN'])
  assert(html.includes('<option value="' + type + '">'), type);
for (const unit of ['GALLON', 'BUCKET', 'SACHET']) assert(html.includes('["'+unit+'"'), unit);
assert(html.includes('loadCatalogItems(1);loadBalances(1)'));
assert(html.includes('var key=OPS.newIdempotencyKey("stock-opening")'));
assert(html.includes('row.stock_state'));
console.log('Inventory patch UI contract checks passed.');
