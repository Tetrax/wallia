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

test.describe("authentification et dialogue", () => {
  test("connexion, question, réponse honnête, citations", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await screenshot(page, testInfo, "01-accueil");

    await newConversation(page);
    await sendMessage(page, "Quelle est la procédure pour le voyant ambre ?");
    await waitForAnswer(page);

    const answer = page.locator(".message-assistant").last();
    await expect(answer).toContainText(/Mode démonstration|voyant|journal/i);
    await screenshot(page, testInfo, "02-reponse");

    const sourcesButton = answer.getByRole("button", { name: /^Sources \(/ });
    if (await sourcesButton.count()) {
      await sourcesButton.click();
      await expect(page.locator(".panel-sources")).toBeVisible();
      await page.locator(".panel-sources .source-card").first().click();
      await expect(page.locator(".panel-sources")).toContainText(/p\.|page/i);
      await screenshot(page, testInfo, "03-sources");
      await page.getByRole("button", { name: "Fermer le panneau" }).click();
    }

    // La bande permanente « corpus fictif » rappelle le statut non officiel.
    await expect(page.getByText(/Corpus démo non officiel/i).first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });

  test("arrêt de génération disponible et état du cas modifiable", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await newConversation(page);
    await sendMessage(page, "Explique la différence entre 10.9 et 10.10, de façon détaillée.");
    await expect(page.locator(".message-streaming")).toBeVisible({ timeout: 30_000 });
    const stop = page.getByTitle("Arrêter la génération");
    if (await stop.isVisible()) {
      await stop.click();
    }
    await expect(page.locator(".message-streaming")).toHaveCount(0, { timeout: 60_000 });
    await expect(page.locator(".message-assistant").last()).toBeVisible();
    await screenshot(page, testInfo, "04-apres-arret");

    await page.getByTitle("État du cas").click();
    await expect(page.locator(".panel-case")).toBeVisible();
    await page.locator(".panel-case input").first().fill("Aster");
    await page.getByRole("button", { name: /Enregistrer l'état du cas|Enregistrer/ }).click();
    await expect(page.locator(".panel-case")).toBeVisible();
    await screenshot(page, testInfo, "05-etat-du-cas");

    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });

  test("recherche documentaire depuis le corpus", async ({ page }, testInfo) => {
    const errors = collectConsoleErrors(page);
    await login(page);
    await newConversation(page);
    await sendMessage(page, "Montre-moi ce que dit le corpus sur la rotation du journal en 10.10.");
    await waitForAnswer(page);
    const answer = page.locator(".message-assistant").last();
    await expect(answer).toBeVisible();
    await screenshot(page, testInfo, "06-recherche");
    await expectNoHorizontalOverflow(page);
    expect(errors, `erreurs console : ${errors.join(" | ")}`).toEqual([]);
  });
});
