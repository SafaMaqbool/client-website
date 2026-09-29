export const site = {
  name: "7YRDS Bau GmbH",
  shortName: "7YRDS Bau",
  description:
    "7YRDS Bau GmbH – Bauunternehmen aus Goch, NRW. Ihr Partner für Generalunternehmerleistungen, Rohbau, Tiefbau, Stahlhallenbau und nachhaltige Energieprojekte.",
  url: "https://www.7yrds-bau.com",
  phone: "02823 976 54-0",
  phoneHref: "tel:+4928239765400",
  email: "info@7yrds-bau.com",
  emailHref: "mailto:info@7yrds-bau.com",
  street: "Von-Monschaw-Str. 12a",
  city: "47574 Goch",
  address: "Von-Monschaw-Str. 12a, 47574 Goch",
  legalName: "7YRDS Bau GmbH",
  registration: "Zertifiziert seit 26.03.2026",
  prequalification: "110.001166",
  vatId: "DE 273679296",
  court: "Amtsgericht Kleve",
  hrb: "HRB 10478",
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
  { label: "Leistungen", href: "/leistungen/generalunternehmer", dropdown: true },
  { label: "Referenzen", href: "/referenzen", dropdown: true },
  { label: "Kontakt", href: "/kontakt" },
];

export const serviceItems = [
  { label: "Generalunternehmerleistungen", href: "/leistungen/generalunternehmer" },
  { label: "Planung und Architektur", href: "/leistungen/planung-architektur" },
  { label: "Rohbauarbeiten", href: "/leistungen/rohbauarbeiten" },
  { label: "Tiefbauarbeiten", href: "/leistungen/tiefbauarbeiten" },
  { label: "Heizung- und Sanitärarbeiten", href: "/leistungen/heizung-sanitaer" },
  { label: "Stahlhallenbau", href: "/leistungen/stahlhallenbau" },
  { label: "Photovoltaikanlagen", href: "/leistungen/photovoltaikanlagen" },
] as const;

export const footerNavigation = [
  { label: "Über uns", href: "/ueber-uns" },
  { label: "Leistungen", href: "/leistungen/generalunternehmer" },
  { label: "Referenzen", href: "/referenzen" },
  { label: "Kontakt", href: "/kontakt" },
] as const;

export const groupCompanies = [
  { label: "7YRDS", domain: "7yrds.com" },
  { label: "7YRDS Energy", domain: "7yrds-energy.com" },
  { label: "7YRDS Real Estate", domain: "7yrds-realestate.com" },
  { label: "7YRDS Consulting", domain: "7yrds-consulting.com" },
  { label: "7YRDS Decon Service", domain: "7yrds-deconservice.com" },
  { label: "7YRDS Protect", domain: "7yrds-protect.com" },
  { label: "7YRDS Service", domain: "7yrds-service.com" },
  { label: "7YRDS XXL Garagen", domain: "7yrds-xxlgaragen.com" },
  { label: "Cyclontec", domain: "cyclontec.com" },
].map((c) => ({ ...c, href: `https://www.${c.domain}` }));
