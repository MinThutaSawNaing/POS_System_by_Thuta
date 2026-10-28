# Product Excel import

Managers and owners can use **Products → Import Excel** to add an existing
catalog without typing products individually. Imports affect only the active
branch and never replace the entire catalog.

## Workflow

1. Download the optional template, or select an existing `.xlsx` / UTF-8 `.csv`
   file. Convert legacy `.xls` workbooks to `.xlsx` first. Put the product list
   on the first data worksheet and store barcodes as text, especially if they
   contain leading zeros or more than 15 digits. Pictures can only be imported
   from `.xlsx` (see [Photos](#photos)).
2. Leave **Skip existing products** selected unless you deliberately want to
   update existing catalog details.
3. Select **Preview import**. Common headings are recognized automatically.
   Use the column selectors to match differently named columns, then preview
   again. New products require a name and selling price.
4. Review the new/update/skip/error counts and every row's proposed action.
   Correct error rows in the original file and upload it again. No rows are
   saved while previewing, and a file containing errors cannot be committed.
5. Choose **Confirm import**. A summary window lists the new, updated, skipped
   and error counts; press **Import products** to write the rows, or
   **Keep reviewing** to go back. Stock quantities never change and new products
   start at zero stock.

Up to 2,000 products and a 15 MB upload are accepted per file. Split larger
catalogs into smaller files.

## Photos

Product pictures travel inside the same workbook, so one file imports catalog
details and photos together.

1. Add a `Photo` column (or `Image` / `Picture`) to the worksheet. Any of these
   headings is recognized automatically and can be remapped in step 3 of the
   dialog.
2. Paste or insert one picture per row so that it sits **inside that row's
   Photo cell**. The picture belongs to the row it is anchored to; the cell
   text stays empty.
3. Preview. Each row that carries a picture shows its file name in the
   detail column, and rows without a picture simply keep their current photo.

Rules and limits:

- One picture per row, at most 2 MB each (`png`, `jpg`, `gif` or `webp`).
  Larger or unsupported pictures are reported as row errors instead of being
  silently dropped.
- **Skip existing products** leaves existing photos untouched. **Update
  existing products** replaces the photo when the row carries a picture and
  leaves it alone when the cell is empty. The replaced file is deleted after
  the import commits.
- Pictures placed *inside* a cell with Excel 365's **Place in Cell** feature
  are not supported; paste them as floating pictures instead. The preview says
  so explicitly when it finds them.
- Previewing never writes image files. If the import fails or is rolled back,
  no new image file is left behind.
- CSV files cannot carry pictures. Use `.xlsx` when photos are required.

## Duplicate policy

- A matching **barcode in the active branch** identifies an existing product.
  Barcodes in other branches do not cause cross-branch updates.
- **Skip** is the default: existing records are not changed.
- **Update** changes only supplied, nonblank catalog fields. Blank cells do not
  erase existing values. Stock, record IDs and catalog identity are kept, and a
  row's picture replaces the existing photo (see [Photos](#photos)).
- A name without a barcode is not safe enough for automatic updates. Existing
  name matches are skipped or flagged; provide the existing barcode to update.
- Conflicting identities, ambiguous names and repeated products in the same
  file are reported rather than resolved by silently overwriting a row.
- A successful import is atomic. If validation or saving fails, the import does
  not leave a partially imported catalog. A changed or expired preview must be
  generated again before saving.

## Stock and related records

This is a **catalog import, not an inventory adjustment**. Existing stock is
unchanged and new products start at zero. Stock columns are ignored with a
warning. Use **Warehouse / Purchase & Receiving** for opening stock and later
stock movements, preserving the existing stock workflow.

Category names are resolved in the current branch. Select **Create missing
categories** if needed; new categories are shown in the preview and created only
on confirmation. Otherwise unknown categories are errors. Units must already exist and be
active in **Settings → Units of Measurement**. Importing does not create new
units or change conversion factors.

## Working with many products at once

The **Products** tab has a checkbox on every row and one in the header:

- The header checkbox selects every product on the current page; ticking rows
  one by one is fine too. The selection is kept while you page or search, and
  the bar above the table shows how many products are selected.
- **Print labels** opens the label picker with the selection already ticked
  (quantities stay editable, and one sheet covers up to 100 products).
- **Export Excel / Export PDF** downloads only the selected products
  (`GET /api/products/export?format=xlsx|pdf&ids=1,2,3`).
- **Set category** moves all selected products into one category, or removes
  their category.
- **Delete** checks every selected product for related records first and lists
  what will be deleted or kept in one window before anything is removed.
- **Clear** drops the selection. Adding, deleting or changing products, and
  switching branch, also clear it.

## MMQR loading feedback

The payment window displays an animated loading indicator while checking
payment readiness, searching for an existing payment, generating the QR and
loading its image. The QR replaces the indicator only when it is ready. An image
failure gives a recovery message rather than leaving a blank payment window.
This is presentation feedback only: payment verification and duplicate-payment
protections remain server-authoritative.