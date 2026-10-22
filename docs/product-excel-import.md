# Product Excel import

Managers and owners can use **Products → Import Excel** to add an existing
catalog without typing products individually. Imports affect only the active
branch and never replace the entire catalog.

## Workflow

1. Download the optional template, or select an existing `.xlsx` / UTF-8 `.csv`
   file. Convert legacy `.xls` workbooks to `.xlsx` first. Put the product list
   on the first data worksheet and store barcodes as text, especially if they
   contain leading zeros or more than 15 digits.
2. Leave **Skip existing products** selected unless you deliberately want to
   update existing catalog details.
3. Select **Preview import**. Common headings are recognized automatically.
   Use the column selectors to match differently named columns, then preview
   again. New products require a name and selling price.
4. Review the new/update/skip/error counts and every row's proposed action.
   Correct error rows in the original file and upload it again. No rows are
   saved while previewing, and a file containing errors cannot be committed.
5. Choose **Confirm import** and approve the final confirmation.

Up to 2,000 products and a 5 MB upload are accepted per file. Split larger
catalogs into smaller files.

## Duplicate policy

- A matching **barcode in the active branch** identifies an existing product.
  Barcodes in other branches do not cause cross-branch updates.
- **Skip** is the default: existing records are not changed.
- **Update** changes only supplied, nonblank catalog fields. Blank cells do not
  erase existing values. Stock, photos, record IDs and catalog identity are kept.
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

## MMQR loading feedback

The payment window displays an animated loading indicator while checking
payment readiness, searching for an existing payment, generating the QR and
loading its image. The QR replaces the indicator only when it is ready. An image
failure gives a recovery message rather than leaving a blank payment window.
This is presentation feedback only: payment verification and duplicate-payment
protections remain server-authoritative.