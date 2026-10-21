# POS barcode scanner quick guide

The POS uses an ordinary USB HID / keyboard-emulation scanner and the server's
exact barcode/SKU lookup API. No special browser driver is needed for a scanner
that types characters and sends a configured Enter or Tab suffix. Keyboard
layouts and suffix settings vary by scanner model.

1. Connect the USB scanner and allow the operating system to identify it as a
   keyboard. If its quick-start sheet requires it, scan the setup barcodes for
   the workstation's keyboard layout (for example, US English) and a single
   Enter suffix. Avoid configuring both Enter and Tab suffixes.
2. Sign in with an account allowed to manage POS orders and open POS order
   entry. Confirm the intended service mode before scanning.
3. Focus the POS barcode input. Keep it focused while scanning a batch; do not
   scan into a guest-name, amount, search, or other text field.
4. Scan one known, active product label. Confirm the exact product name and
   price appear in the cart. Scan it again to confirm the current scan workflow
   increments the quantity once. Unknown or inactive codes must show an error;
   do not submit the cart until the item is resolved.
5. Test a deliberately unknown code and confirm it is not added. If scans are
   being treated as form submission or characters are missing, check the
   keyboard-layout and suffix settings and focus the barcode field again.

A code label being printable does not prove a physical scanner has read it.
The automated suite exercises keyboard-emulation input, exact lookup, duplicate
scan handling, and cart quantity behavior. No physical USB scanner was
connected for this update, so device-specific layout/suffix compatibility still
needs a real-station check.
