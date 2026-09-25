import { expect, test } from "@playwright/test";

import {
  collectConsoleErrors,
  expectNoHorizontalOverflow,
  login,
  screenshot,
} from "../helpers";

test.describe("bibliothèque documentaire et jobs", () => {
  test("lister les documents, inspecter les passages, voir les jobs", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await page.getByRole("button", { name: /Bibliothèque & jobs/ }).click();
    await expect(page.getByRole("heading", { name: "Bibliothèque documentaire" })).toBeVisible();

    const table = page.locator("table.table").first();
    await expect(table).toBeVisible({ timeout: 30_000 });

    const rows = page.locator("table.table tbody tr");
    const count = await rows.count();
    test.skip(count === 0, "corpus non importé : lancer scripts/acceptance.sh corpus au préalable");

    await expect(page.locator("tbody").first()).toContainText(/démo non officiel|officiel/);
    await screenshot(page, testInfo, "10-bibliotheque");

    // Inspecter les passages d'un document indexé.
    const passagesButton = page.getByRole("button", { name: "Passages" }).first();
    await passagesButton.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByRole("dialog")).toContainText(/Passages/);
    await screenshot(page, testInfo, "11-passages");
    await page.getByRole("button", { name: "Fermer" }).first().click();

    // Section jobs d'ingestion.
    await expect(page.getByRole("heading", { name: "Jobs d'ingestion" })).toBeVisible();
    await expect(page.locator("body")).toContainText(/done|queued|failed|running|cancelled/);

    // Filtres réellement fonctionnels.
    await page.locator('.filters select').first().selectOption("demo");
    await expect(page.locator("tbody tr").first()).toBeVisible();

    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });

  test("formulaire d'import refusant un fichier non PDF", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await page.getByRole("button", { name: /Bibliothèque & jobs/ }).click();
    await page.getByRole("button", { name: /Importer un PDF/ }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.locator('input[type="file"]').setInputFiles({
      name: "note.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("ceci n'est pas un PDF"),
    });
    await dialog.locator('input[type="text"]').first().fill("Test e2e refus");
    await dialog.locator('button[type="submit"]').click();
    await expect(dialog).toContainText(/PDF|refus|invalide/i);
    await screenshot(page, testInfo, "12-import-refus");
    await page.getByRole("button", { name: "Fermer" }).first().click();
    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });
});
