/** Approved custom round @tg artwork, generated with ImageGen. */
import { readFileSync } from "node:fs";

const asset = (name: string): Buffer => readFileSync(new URL(`../assets/${name}`, import.meta.url));

export const TG_ICON_PNG_32 = asset("favicon-32.png");
export const TG_ICON_PNG_128 = asset("icon-128.png");
export const TG_ICON_PNG_256 = asset("icon-256.png");
export const TG_ICON_PNG_512 = asset("icon-512.png");
export const TG_TOOL_ICON_PNG = asset("tool-icon.png");
export const TG_FAVICON_ICO = asset("favicon.ico");
export const TG_TOUCH_ICON_PNG = asset("apple-touch-icon.png");
