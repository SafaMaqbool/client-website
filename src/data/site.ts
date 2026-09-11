export const site = {
  name: "Bauunternehmen Goch",
  shortName: "Bauunternehmen Goch",
  description:
    "Bauunternehmen aus Goch, NRW – Ihr Partner für Generalunternehmerleistungen, Rohbau, Tiefbau, Stahlhallenbau und nachhaltige Energieprojekte.",
  url: "https://www.bauunternehmen-goch.de",
  phone: "+49 (0) 2842 12345",
  phoneHref: "tel:+49284212345",
  email: "info@bauunternehmen-goch.de",
  emailHref: "mailto:info@bauunternehmen-goch.de",
  address: "Hauptstraße 24, 47574 Goch, NRW",
  legalName: "Bauunternehmen Goch GmbH",
  registration: "Zertifiziert seit 26.03.2026",
  vatId: "DE123456789",
  openingHours: "Mo.–Fr. 08:00–17:00",
} as const;

type NavItem = {
  label: string;
  href: string;
  dropdown?: boolean;
};

export const navItems: ReadonlyArray<NavItem> = [
  { label: "Home", href: "/" },
  { label: "Über uns", href: "/ueber-uns" },
  { label: "Leistungen", href: "/leistungen", dropdown: true },
  { label: "Referenzen", href: "/referenzen", dropdown: true },
  { label: "Kontakt", href: "/kontakt" },
];

export const serviceItems = [
  "Generalunternehmerleistungen",
  "Planung und Architektur",
  "Rohbauarbeiten",
  "Tiefbauarbeiten",
  "Heizung- und Sanitärarbeiten",
  "Stahlhallenbau",
  "Photovoltaikanlagen",
] as const;

export const footerNavigation = [
  { label: "Über uns", href: "/ueber-uns" },
  { label: "Leistungen", href: "/leistungen" },
  { label: "Referenzen", href: "/referenzen" },
  { label: "Kontakt", href: "/kontakt" },
] as const;
