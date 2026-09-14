export const THEME_OPTIONS = [
  {
    name: "Default",
    label: "Jarvs Spotlight",
    description: "Quiet charcoal surfaces and a clean, focused conversation.",
    attribute: "default",
  },
  {
    name: "Dark",
    label: "Dark",
    description: "A neutral, minimal dark theme with restrained contrast.",
    attribute: "dark",
  },
  {
    name: "Light",
    label: "Light",
    description: "High-contrast light surfaces for bright environments.",
    attribute: "light",
  },
  {
    name: "Glassy Windows",
    label: "Glassy Windows",
    description: "Translucent panels, cool highlights, and frosted-glass depth.",
    attribute: "glassy",
  },
  {
    name: "AI Vaporwave",
    label: "AI Vaporwave",
    description: "A vivid blue, purple, and pink gradient.",
    attribute: "vaporwave",
  },
] as const;

export type ThemeName = (typeof THEME_OPTIONS)[number]["name"];
export type ThemeAttribute = (typeof THEME_OPTIONS)[number]["attribute"];

const THEME_ALIASES: Record<string, ThemeName> = {
  default: "Default",
  jarvs: "Default",
  dark: "Dark",
  light: "Light",
  glass: "Glassy Windows",
  glassy: "Glassy Windows",
  "glassy windows": "Glassy Windows",
  vaporwave: "AI Vaporwave",
  "ai vaporwave": "AI Vaporwave",
};

export function normalizeThemeName(value: unknown): ThemeName {
  if (typeof value !== "string") return "Default";
  return THEME_ALIASES[value.trim().toLowerCase()] ?? "Default";
}

export function getThemeAttribute(theme: ThemeName): ThemeAttribute {
  return THEME_OPTIONS.find((option) => option.name === theme)?.attribute ?? "default";
}
