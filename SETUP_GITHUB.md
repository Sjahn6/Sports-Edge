# Put SportsEdge online (free, about 20 minutes, once)

When you're done, the dashboard lives at `https://sjahn6.github.io/sportsedge/`,
updates itself four times a day, and works on any phone. Your Mac can be off.

## 1. Sign in

Sign in at https://github.com as **sjahn6**.

## 2. Make the project and upload the files

1. Click the **+** at the top right, then **New repository**.
2. Name it `sportsedge`. Leave it **Public** (free web pages need that; the link is just unlisted).
3. Click **Create repository**.
4. On the next page, click the link **uploading an existing file**.
5. Unzip SportsEdge.zip. Open the SportsEdge folder, select everything inside it
   (Cmd+A), and drag it into the browser window.
6. Wait for the files to finish loading, then click **Commit changes** at the bottom.

## 3. Add the schedule

Mac hides the folder GitHub needs for this, so you'll paste it in once:

1. Click the **Actions** tab at the top of your project.
2. Click **set up a workflow yourself**.
3. Delete everything in the editor. Open `GITHUB_WORKFLOW.yml` from the SportsEdge folder
   (TextEdit is fine), copy all of it, and paste it into the editor.
4. Click **Commit changes...**, then **Commit changes** again.

## 4. Turn on the web page

1. Click the **Settings** tab, then **Pages** in the left menu.
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.

## 5. Run it the first time

1. Click the **Actions** tab, then **Update dashboard** on the left.
2. Click **Run workflow**, then the green **Run workflow** button.
3. The first run loads two seasons for all three leagues and takes about 10 minutes.
   A green check means it worked.
4. Open `https://sjahn6.github.io/sportsedge/`.

On your iPhone, open that address in Safari, tap **Share**, then **Add to Home Screen**.
It shows up like an app.

## After that

* It updates on its own around 9am, 5pm and 6:45pm every day, plus 11:45am on Sundays.
  The **Run workflow** button refreshes it any time.
* To change something, ask Claude. You'll get updated files: open the file on GitHub,
  click the pencil (or **Add file > Upload files** to replace it), and commit. The page
  updates on the next run.
* Share the link with friends and they see the same dashboard.
* If a run shows a red X, click it, copy the error, and paste it to Claude.
