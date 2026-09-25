import fs from "node:fs";
import path from "node:path";
import type { Page, TestInfo } from "@playwright/test";
import { expect } from "@playwright/test";

export const EVIDENCE_DIR = path.resolve(
  process.env.WALLIA_EVIDENCE_DIR ?? path.join(__dirname, "..", "..", "runtime", "evidence"),
);
export const CREDENTIALS_FILE = path.resolve(
  process.env.WALLIA_E2E_CREDENTIALS ?? path.join(__dirname, "..", "..", "runtime", "initial-access.txt"),
);

export interface Credentials {
  email: string;
  password: string;
}

/** Lit les identifiants depuis le fichier d'accès initial — jamais affichés. */
export function credentials(): Credentials {
  const raw = fs.readFileSync(CREDENTIALS_FILE, "utf8");
  let email = "";
  let password = "";
  for (const line of raw.split("\n")) {
    const index = line.indexOf(":");
    if (index < 0) continue;
    const key = line.slice(0, index).trim().toLowerCase();
    const value = line.slice(index + 1).trim();
    if (key === "email") email = value;
    if (key === "password") password = value;
  }
  if (!email || !password) throw new Error(`identifiants illisibles dans ${CREDENTIALS_FILE}`);
  return { email, password };
}

export function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  return errors;
}

export async function login(page: Page): Promise<void> {
  const { email, password } = credentials();
  await page.goto("/");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.getByRole("button", { name: "Se connecter" }).click();
  await expect(page.getByRole("button", { name: /Nouvelle conversation/ })).toBeVisible();
}

export async function screenshot(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  const file = path.join(EVIDENCE_DIR, `${name}-${testInfo.project.name}.png`);
  await page.screenshot({ path: file, fullPage: true });
}

/** Aucun débordement horizontal : la page tient dans la largeur du viewport. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(
    overflow.scrollWidth,
    `débordement horizontal (${overflow.scrollWidth}px > ${overflow.innerWidth}px)`,
  ).toBeLessThanOrEqual(overflow.innerWidth + 2);
}

export async function newConversation(page: Page): Promise<void> {
  await page.getByRole("button", { name: /Nouvelle conversation/ }).click();
  await expect(page.locator("textarea")).toBeVisible();
}

export async function sendMessage(page: Page, text: string): Promise<void> {
  await page.locator("textarea").fill(text);
  await page.getByTitle("Envoyer").click();
}

/** Attend la fin de la génération (statut « done » côté interface). */
export async function waitForAnswer(page: Page): Promise<void> {
  await expect(page.locator(".message-assistant").last()).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".message-streaming")).toHaveCount(0, { timeout: 60_000 });
}
