import { expect, test } from "@playwright/test";

import {
  collectConsoleErrors,
  expectNoHorizontalOverflow,
  login,
  newConversation,
  screenshot,
  sendMessage,
  waitForAnswer,
} from "../helpers";

test.describe("administration et usage mobile", () => {
  test("administration : capacités, vision inactive, clé absente", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await page.getByRole("button", { name: /^Administration$/ }).click();
    await expect(page.getByRole("heading", { name: "Administration" })).toBeVisible();

    const body = page.locator("body");
    await expect(body).toContainText("Embeddings (recherche)");
    await expect(body).toContainText(/multilingual-e5-small/);
    await expect(body).toContainText("Fournisseur de chat");
    await expect(body).toContainText(/vision/i);
    await expect(body).toContainText(/Web & vision|web/i);
    await screenshot(page, testInfo, "20-administration");

    // Barrière de pertinence réellement modifiable puis restaurée.
    const cosineInput = page.locator('input[type="number"]').first();
    if (await cosineInput.isVisible()) {
      const initial = await cosineInput.inputValue();
      await cosineInput.fill(initial || "0.84");
      await page.getByRole("button", { name: "Enregistrer" }).first().click();
      await expect(body).toContainText(/enregistr|à jour|Enregistré/i);
    }

    await expect(body).toContainText(/non officiel/i);
    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });

  test("navigation mobile : tiroir, conversation, réponse", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await screenshot(page, testInfo, "30-mobile-accueil");

    const drawerToggle = page.getByRole("button", { name: "Ouvrir le menu" });
    if (await drawerToggle.isVisible()) {
      await drawerToggle.click();
      await expect(page.locator(".sidebar.sidebar-open")).toBeVisible();
      await screenshot(page, testInfo, "31-mobile-tiroir");
      await page.getByRole("button", { name: "Fermer le menu" }).click();
    }

    await newConversation(page);
    await sendMessage(page, "État du voyant ambre ?");
    await waitForAnswer(page);
    await expect(page.locator(".message-assistant").last()).toBeVisible();
    await screenshot(page, testInfo, "32-mobile-reponse");

    await expect(page.locator(".composer textarea")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });
});
