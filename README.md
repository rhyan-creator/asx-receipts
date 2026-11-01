# ASX Receipts

A website that charts **cash receipts from customers** for any ASX company over about the last ten years.

The figures come from:
- line 1.1 of each Appendix 4C or 5B (quarterly)
- the cash flow statement in each half-year and annual report

**It keeps itself up to date.** A free GitHub robot checks the ASX every few hours for new filings and adds them. You don't run anything, and there's nothing to install.

---

## One-time setup (about 10 minutes, no coding)

Use Chrome or Edge for these steps.

**1. Make a free GitHub account** at <https://github.com/signup>.

**2. Create an empty repository.**
- Click the **+** (top right) → **New repository**.
- Name it `asx-receipts`.
- Choose **Public**. Free hosting needs it; the data is public ASX filings.
- Click **Create repository**.

**3. Upload these files.**
- On the new page, click the link **uploading an existing file**.
- Open the unzipped `asx-receipts` folder and select **everything inside it**: `docs`, `scraper`, `state`, `tests`, `.github` and the loose files.
- Drag them all onto the GitHub page, then click **Commit changes**.
- On a Mac, the `.github` folder is hidden. Press **Cmd + Shift + .** in Finder to show it.

**4. Turn on the website.**
- Go to **Settings** → **Pages**.
- Under *Build and deployment* → *Source*, choose **GitHub Actions**.

**5. Start the robot.**
- Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
- Click **Update data** on the left, then **Run workflow** on the right.
- In the box, type your first tickers, e.g. `LGI C79 LBL SPZ NOL CAF`.
- Press the green **Run workflow** button.

**6. Open your site.** After about 10 minutes the run shows a green tick. Your site is at:

`https://YOUR-GITHUB-USERNAME.github.io/asx-receipts/`

The link also appears in **Settings → Pages**. Bookmark it, since it works on your phone too.

From here the robot runs by itself every 3 hours:
- **First 2–3 days:** it loads ten years of history for the rest of the ASX, one chunk per run.
- **After that:** it checks every company for new announcements roughly twice a day.

---

## Using it

- **Look up a company:** type a ticker or company name and press Enter.
- **Change the view:** switch between Quarterly, Half-yearly, Annual and TTM, and between 5y, 10y and All.
- **Check a figure:** hover a bar to see the value, YoY growth, whether it was reported or derived (e.g. H2 = FY − H1), and the filing it came from.
- **See all the filings:** the **Source filings** tab links every filing to its PDF on the ASX site.
- **Export:** **Download CSV** gives you the figures in the current view.
- **Share or bookmark:** each company has its own link, e.g. `…/asx-receipts/#LGI`.

## Need a company that hasn't loaded yet?

Go to **Actions → Update data → Run workflow**, type the ticker, and run it. It's on the site within about 5 minutes. The site also gives you a link to this page when you search for a company that isn't loaded.

## Change what loads first

Edit `watchlist.txt` on GitHub: open the file, click the pencil, and put one ticker per line. Tickers in this file are loaded before the rest of the ASX.

## Fix a figure

If a filing was misread or a figure is missing, open `overrides.csv` on GitHub, click the pencil, and add a line. Then commit. The value is in dollars:

```
ticker,period_end,months,value
LGI,30/09/2025,3,4512000
```

Your figure replaces the scraped one at the next update.

## Limits

- **Banks and indirect-method reporters** don't disclose receipts from customers, so there's nothing to chart for them.
- **Foreign-currency reporters** (e.g. those reporting in US$) are shown as reported, without conversion. The site warns you when the currency changes.
- **Scanned or unusual PDFs** are occasionally skipped. The Source filings tab shows which ones were read.
- **The robot pauses** if it fails repeatedly (e.g. the ASX blocks it). GitHub emails you when a run fails, and the Actions tab shows the log.

---

## Appendix: if the robot doesn't appear under Actions

The `.github` folder didn't upload. You can create the file by hand:
1. Click **Add file → Create new file**.
2. Name it `.github/workflows/update.yml`.
3. Paste in the contents of that file from the zip.
4. Commit.
