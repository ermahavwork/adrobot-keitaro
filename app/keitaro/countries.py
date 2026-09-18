"""Справочник стран ISO 3166-1 для фильтра потока `country` в Keitaro.

Keitaro молча принимает в фильтре стран любые строки: опечатка вроде "UK" вместо
"GB" сохраняется без ошибки, а поток просто перестаёт получать трафик из нужной
страны. Поэтому коды проверяем сами, до отправки в трекер.

В модуле:
- COUNTRIES — все 249 официально присвоенных кодов alpha-2 с названиями (en, ru);
- ALIASES — коды alpha-3 и частые ошибочные обозначения ("UK", "USA", "UAE");
- normalize_country_code / parse_geo_input — разбор пользовательского ввода;
- country_name / search_countries — отображение и автокомплит.

Неприсвоенные и выведенные из обращения коды (XK, AN, CS и т.п.) намеренно
отсутствуют. Только стандартная библиотека, Python 3.10+.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "ALIASES",
    "COUNTRIES",
    "country_name",
    "normalize_country_code",
    "parse_geo_input",
    "search_countries",
]

COUNTRIES: dict[str, tuple[str, str]] = {
    "AD": ("Andorra", "Андорра"),
    "AE": ("United Arab Emirates", "ОАЭ"),
    "AF": ("Afghanistan", "Афганистан"),
    "AG": ("Antigua and Barbuda", "Антигуа и Барбуда"),
    "AI": ("Anguilla", "Ангилья"),
    "AL": ("Albania", "Албания"),
    "AM": ("Armenia", "Армения"),
    "AO": ("Angola", "Ангола"),
    "AQ": ("Antarctica", "Антарктида"),
    "AR": ("Argentina", "Аргентина"),
    "AS": ("American Samoa", "Американское Самоа"),
    "AT": ("Austria", "Австрия"),
    "AU": ("Australia", "Австралия"),
    "AW": ("Aruba", "Аруба"),
    "AX": ("Åland Islands", "Аландские острова"),
    "AZ": ("Azerbaijan", "Азербайджан"),
    "BA": ("Bosnia and Herzegovina", "Босния и Герцеговина"),
    "BB": ("Barbados", "Барбадос"),
    "BD": ("Bangladesh", "Бангладеш"),
    "BE": ("Belgium", "Бельгия"),
    "BF": ("Burkina Faso", "Буркина-Фасо"),
    "BG": ("Bulgaria", "Болгария"),
    "BH": ("Bahrain", "Бахрейн"),
    "BI": ("Burundi", "Бурунди"),
    "BJ": ("Benin", "Бенин"),
    "BL": ("Saint Barthélemy", "Сен-Бартелеми"),
    "BM": ("Bermuda", "Бермудские острова"),
    "BN": ("Brunei", "Бруней"),
    "BO": ("Bolivia", "Боливия"),
    "BQ": ("Caribbean Netherlands", "Карибские Нидерланды"),
    "BR": ("Brazil", "Бразилия"),
    "BS": ("Bahamas", "Багамы"),
    "BT": ("Bhutan", "Бутан"),
    "BV": ("Bouvet Island", "Остров Буве"),
    "BW": ("Botswana", "Ботсвана"),
    "BY": ("Belarus", "Беларусь"),
    "BZ": ("Belize", "Белиз"),
    "CA": ("Canada", "Канада"),
    "CC": ("Cocos Islands", "Кокосовые острова"),
    "CD": ("DR Congo", "ДР Конго"),
    "CF": ("Central African Republic", "ЦАР"),
    "CG": ("Republic of the Congo", "Республика Конго"),
    "CH": ("Switzerland", "Швейцария"),
    "CI": ("Ivory Coast", "Кот-д'Ивуар"),
    "CK": ("Cook Islands", "Острова Кука"),
    "CL": ("Chile", "Чили"),
    "CM": ("Cameroon", "Камерун"),
    "CN": ("China", "Китай"),
    "CO": ("Colombia", "Колумбия"),
    "CR": ("Costa Rica", "Коста-Рика"),
    "CU": ("Cuba", "Куба"),
    "CV": ("Cape Verde", "Кабо-Верде"),
    "CW": ("Curaçao", "Кюрасао"),
    "CX": ("Christmas Island", "Остров Рождества"),
    "CY": ("Cyprus", "Кипр"),
    "CZ": ("Czechia", "Чехия"),
    "DE": ("Germany", "Германия"),
    "DJ": ("Djibouti", "Джибути"),
    "DK": ("Denmark", "Дания"),
    "DM": ("Dominica", "Доминика"),
    "DO": ("Dominican Republic", "Доминиканская Республика"),
    "DZ": ("Algeria", "Алжир"),
    "EC": ("Ecuador", "Эквадор"),
    "EE": ("Estonia", "Эстония"),
    "EG": ("Egypt", "Египет"),
    "EH": ("Western Sahara", "Западная Сахара"),
    "ER": ("Eritrea", "Эритрея"),
    "ES": ("Spain", "Испания"),
    "ET": ("Ethiopia", "Эфиопия"),
    "FI": ("Finland", "Финляндия"),
    "FJ": ("Fiji", "Фиджи"),
    "FK": ("Falkland Islands", "Фолклендские острова"),
    "FM": ("Micronesia", "Микронезия"),
    "FO": ("Faroe Islands", "Фарерские острова"),
    "FR": ("France", "Франция"),
    "GA": ("Gabon", "Габон"),
    "GB": ("United Kingdom", "Великобритания"),
    "GD": ("Grenada", "Гренада"),
    "GE": ("Georgia", "Грузия"),
    "GF": ("French Guiana", "Французская Гвиана"),
    "GG": ("Guernsey", "Гернси"),
    "GH": ("Ghana", "Гана"),
    "GI": ("Gibraltar", "Гибралтар"),
    "GL": ("Greenland", "Гренландия"),
    "GM": ("Gambia", "Гамбия"),
    "GN": ("Guinea", "Гвинея"),
    "GP": ("Guadeloupe", "Гваделупа"),
    "GQ": ("Equatorial Guinea", "Экваториальная Гвинея"),
    "GR": ("Greece", "Греция"),
    "GS": (
        "South Georgia and the South Sandwich Islands",
        "Южная Георгия и Южные Сандвичевы острова",
    ),
    "GT": ("Guatemala", "Гватемала"),
    "GU": ("Guam", "Гуам"),
    "GW": ("Guinea-Bissau", "Гвинея-Бисау"),
    "GY": ("Guyana", "Гайана"),
    "HK": ("Hong Kong", "Гонконг"),
    "HM": ("Heard Island and McDonald Islands", "Остров Херд и острова Макдональд"),
    "HN": ("Honduras", "Гондурас"),
    "HR": ("Croatia", "Хорватия"),
    "HT": ("Haiti", "Гаити"),
    "HU": ("Hungary", "Венгрия"),
    "ID": ("Indonesia", "Индонезия"),
    "IE": ("Ireland", "Ирландия"),
    "IL": ("Israel", "Израиль"),
    "IM": ("Isle of Man", "Остров Мэн"),
    "IN": ("India", "Индия"),
    "IO": ("British Indian Ocean Territory", "Британская территория в Индийском океане"),
    "IQ": ("Iraq", "Ирак"),
    "IR": ("Iran", "Иран"),
    "IS": ("Iceland", "Исландия"),
    "IT": ("Italy", "Италия"),
    "JE": ("Jersey", "Джерси"),
    "JM": ("Jamaica", "Ямайка"),
    "JO": ("Jordan", "Иордания"),
    "JP": ("Japan", "Япония"),
    "KE": ("Kenya", "Кения"),
    "KG": ("Kyrgyzstan", "Киргизия"),
    "KH": ("Cambodia", "Камбоджа"),
    "KI": ("Kiribati", "Кирибати"),
    "KM": ("Comoros", "Коморы"),
    "KN": ("Saint Kitts and Nevis", "Сент-Китс и Невис"),
    "KP": ("North Korea", "Северная Корея"),
    "KR": ("South Korea", "Южная Корея"),
    "KW": ("Kuwait", "Кувейт"),
    "KY": ("Cayman Islands", "Каймановы острова"),
    "KZ": ("Kazakhstan", "Казахстан"),
    "LA": ("Laos", "Лаос"),
    "LB": ("Lebanon", "Ливан"),
    "LC": ("Saint Lucia", "Сент-Люсия"),
    "LI": ("Liechtenstein", "Лихтенштейн"),
    "LK": ("Sri Lanka", "Шри-Ланка"),
    "LR": ("Liberia", "Либерия"),
    "LS": ("Lesotho", "Лесото"),
    "LT": ("Lithuania", "Литва"),
    "LU": ("Luxembourg", "Люксембург"),
    "LV": ("Latvia", "Латвия"),
    "LY": ("Libya", "Ливия"),
    "MA": ("Morocco", "Марокко"),
    "MC": ("Monaco", "Монако"),
    "MD": ("Moldova", "Молдова"),
    "ME": ("Montenegro", "Черногория"),
    "MF": ("Saint Martin", "Сен-Мартен"),
    "MG": ("Madagascar", "Мадагаскар"),
    "MH": ("Marshall Islands", "Маршалловы острова"),
    "MK": ("North Macedonia", "Северная Македония"),
    "ML": ("Mali", "Мали"),
    "MM": ("Myanmar", "Мьянма"),
    "MN": ("Mongolia", "Монголия"),
    "MO": ("Macau", "Макао"),
    "MP": ("Northern Mariana Islands", "Северные Марианские острова"),
    "MQ": ("Martinique", "Мартиника"),
    "MR": ("Mauritania", "Мавритания"),
    "MS": ("Montserrat", "Монтсеррат"),
    "MT": ("Malta", "Мальта"),
    "MU": ("Mauritius", "Маврикий"),
    "MV": ("Maldives", "Мальдивы"),
    "MW": ("Malawi", "Малави"),
    "MX": ("Mexico", "Мексика"),
    "MY": ("Malaysia", "Малайзия"),
    "MZ": ("Mozambique", "Мозамбик"),
    "NA": ("Namibia", "Намибия"),
    "NC": ("New Caledonia", "Новая Каледония"),
    "NE": ("Niger", "Нигер"),
    "NF": ("Norfolk Island", "Остров Норфолк"),
    "NG": ("Nigeria", "Нигерия"),
    "NI": ("Nicaragua", "Никарагуа"),
    "NL": ("Netherlands", "Нидерланды"),
    "NO": ("Norway", "Норвегия"),
    "NP": ("Nepal", "Непал"),
    "NR": ("Nauru", "Науру"),
    "NU": ("Niue", "Ниуэ"),
    "NZ": ("New Zealand", "Новая Зеландия"),
    "OM": ("Oman", "Оман"),
    "PA": ("Panama", "Панама"),
    "PE": ("Peru", "Перу"),
    "PF": ("French Polynesia", "Французская Полинезия"),
    "PG": ("Papua New Guinea", "Папуа — Новая Гвинея"),
    "PH": ("Philippines", "Филиппины"),
    "PK": ("Pakistan", "Пакистан"),
    "PL": ("Poland", "Польша"),
    "PM": ("Saint Pierre and Miquelon", "Сен-Пьер и Микелон"),
    "PN": ("Pitcairn Islands", "Острова Питкэрн"),
    "PR": ("Puerto Rico", "Пуэрто-Рико"),
    "PS": ("Palestine", "Палестина"),
    "PT": ("Portugal", "Португалия"),
    "PW": ("Palau", "Палау"),
    "PY": ("Paraguay", "Парагвай"),
    "QA": ("Qatar", "Катар"),
    "RE": ("Réunion", "Реюньон"),
    "RO": ("Romania", "Румыния"),
    "RS": ("Serbia", "Сербия"),
    "RU": ("Russia", "Россия"),
    "RW": ("Rwanda", "Руанда"),
    "SA": ("Saudi Arabia", "Саудовская Аравия"),
    "SB": ("Solomon Islands", "Соломоновы острова"),
    "SC": ("Seychelles", "Сейшелы"),
    "SD": ("Sudan", "Судан"),
    "SE": ("Sweden", "Швеция"),
    "SG": ("Singapore", "Сингапур"),
    "SH": ("Saint Helena", "Остров Святой Елены"),
    "SI": ("Slovenia", "Словения"),
    "SJ": ("Svalbard and Jan Mayen", "Шпицберген и Ян-Майен"),
    "SK": ("Slovakia", "Словакия"),
    "SL": ("Sierra Leone", "Сьерра-Леоне"),
    "SM": ("San Marino", "Сан-Марино"),
    "SN": ("Senegal", "Сенегал"),
    "SO": ("Somalia", "Сомали"),
    "SR": ("Suriname", "Суринам"),
    "SS": ("South Sudan", "Южный Судан"),
    "ST": ("São Tomé and Príncipe", "Сан-Томе и Принсипи"),
    "SV": ("El Salvador", "Сальвадор"),
    "SX": ("Sint Maarten", "Синт-Мартен"),
    "SY": ("Syria", "Сирия"),
    "SZ": ("Eswatini", "Эсватини"),
    "TC": ("Turks and Caicos Islands", "Теркс и Кайкос"),
    "TD": ("Chad", "Чад"),
    "TF": ("French Southern Territories", "Французские Южные территории"),
    "TG": ("Togo", "Того"),
    "TH": ("Thailand", "Таиланд"),
    "TJ": ("Tajikistan", "Таджикистан"),
    "TK": ("Tokelau", "Токелау"),
    "TL": ("Timor-Leste", "Восточный Тимор"),
    "TM": ("Turkmenistan", "Туркменистан"),
    "TN": ("Tunisia", "Тунис"),
    "TO": ("Tonga", "Тонга"),
    "TR": ("Turkey", "Турция"),
    "TT": ("Trinidad and Tobago", "Тринидад и Тобаго"),
    "TV": ("Tuvalu", "Тувалу"),
    "TW": ("Taiwan", "Тайвань"),
    "TZ": ("Tanzania", "Танзания"),
    "UA": ("Ukraine", "Украина"),
    "UG": ("Uganda", "Уганда"),
    "UM": ("United States Minor Outlying Islands", "Внешние малые острова США"),
    "US": ("United States", "США"),
    "UY": ("Uruguay", "Уругвай"),
    "UZ": ("Uzbekistan", "Узбекистан"),
    "VA": ("Vatican City", "Ватикан"),
    "VC": ("Saint Vincent and the Grenadines", "Сент-Винсент и Гренадины"),
    "VE": ("Venezuela", "Венесуэла"),
    "VG": ("British Virgin Islands", "Британские Виргинские острова"),
    "VI": ("U.S. Virgin Islands", "Виргинские острова США"),
    "VN": ("Vietnam", "Вьетнам"),
    "VU": ("Vanuatu", "Вануату"),
    "WF": ("Wallis and Futuna", "Уоллис и Футуна"),
    "WS": ("Samoa", "Самоа"),
    "YE": ("Yemen", "Йемен"),
    "YT": ("Mayotte", "Майотта"),
    "ZA": ("South Africa", "ЮАР"),
    "ZM": ("Zambia", "Замбия"),
    "ZW": ("Zimbabwe", "Зимбабве"),
}

_ALPHA3_TO_ALPHA2: dict[str, str] = {
    "AND": "AD", "ARE": "AE", "AFG": "AF", "ATG": "AG", "AIA": "AI", "ALB": "AL", "ARM": "AM",
    "AGO": "AO", "ATA": "AQ", "ARG": "AR", "ASM": "AS", "AUT": "AT", "AUS": "AU", "ABW": "AW",
    "ALA": "AX", "AZE": "AZ",
    "BIH": "BA", "BRB": "BB", "BGD": "BD", "BEL": "BE", "BFA": "BF", "BGR": "BG", "BHR": "BH",
    "BDI": "BI", "BEN": "BJ", "BLM": "BL", "BMU": "BM", "BRN": "BN", "BOL": "BO", "BES": "BQ",
    "BRA": "BR", "BHS": "BS", "BTN": "BT", "BVT": "BV", "BWA": "BW", "BLR": "BY", "BLZ": "BZ",
    "CAN": "CA", "CCK": "CC", "COD": "CD", "CAF": "CF", "COG": "CG", "CHE": "CH", "CIV": "CI",
    "COK": "CK", "CHL": "CL", "CMR": "CM", "CHN": "CN", "COL": "CO", "CRI": "CR", "CUB": "CU",
    "CPV": "CV", "CUW": "CW", "CXR": "CX", "CYP": "CY", "CZE": "CZ",
    "DEU": "DE", "DJI": "DJ", "DNK": "DK", "DMA": "DM", "DOM": "DO", "DZA": "DZ",
    "ECU": "EC", "EST": "EE", "EGY": "EG", "ESH": "EH", "ERI": "ER", "ESP": "ES", "ETH": "ET",
    "FIN": "FI", "FJI": "FJ", "FLK": "FK", "FSM": "FM", "FRO": "FO", "FRA": "FR",
    "GAB": "GA", "GBR": "GB", "GRD": "GD", "GEO": "GE", "GUF": "GF", "GGY": "GG", "GHA": "GH",
    "GIB": "GI", "GRL": "GL", "GMB": "GM", "GIN": "GN", "GLP": "GP", "GNQ": "GQ", "GRC": "GR",
    "SGS": "GS", "GTM": "GT", "GUM": "GU", "GNB": "GW", "GUY": "GY",
    "HKG": "HK", "HMD": "HM", "HND": "HN", "HRV": "HR", "HTI": "HT", "HUN": "HU",
    "IDN": "ID", "IRL": "IE", "ISR": "IL", "IMN": "IM", "IND": "IN", "IOT": "IO", "IRQ": "IQ",
    "IRN": "IR", "ISL": "IS", "ITA": "IT",
    "JEY": "JE", "JAM": "JM", "JOR": "JO", "JPN": "JP",
    "KEN": "KE", "KGZ": "KG", "KHM": "KH", "KIR": "KI", "COM": "KM", "KNA": "KN", "PRK": "KP",
    "KOR": "KR", "KWT": "KW", "CYM": "KY", "KAZ": "KZ",
    "LAO": "LA", "LBN": "LB", "LCA": "LC", "LIE": "LI", "LKA": "LK", "LBR": "LR", "LSO": "LS",
    "LTU": "LT", "LUX": "LU", "LVA": "LV", "LBY": "LY",
    "MAR": "MA", "MCO": "MC", "MDA": "MD", "MNE": "ME", "MAF": "MF", "MDG": "MG", "MHL": "MH",
    "MKD": "MK", "MLI": "ML", "MMR": "MM", "MNG": "MN", "MAC": "MO", "MNP": "MP", "MTQ": "MQ",
    "MRT": "MR", "MSR": "MS", "MLT": "MT", "MUS": "MU", "MDV": "MV", "MWI": "MW", "MEX": "MX",
    "MYS": "MY", "MOZ": "MZ",
    "NAM": "NA", "NCL": "NC", "NER": "NE", "NFK": "NF", "NGA": "NG", "NIC": "NI", "NLD": "NL",
    "NOR": "NO", "NPL": "NP", "NRU": "NR", "NIU": "NU", "NZL": "NZ",
    "OMN": "OM",
    "PAN": "PA", "PER": "PE", "PYF": "PF", "PNG": "PG", "PHL": "PH", "PAK": "PK", "POL": "PL",
    "SPM": "PM", "PCN": "PN", "PRI": "PR", "PSE": "PS", "PRT": "PT", "PLW": "PW", "PRY": "PY",
    "QAT": "QA",
    "REU": "RE", "ROU": "RO", "SRB": "RS", "RUS": "RU", "RWA": "RW",
    "SAU": "SA", "SLB": "SB", "SYC": "SC", "SDN": "SD", "SWE": "SE", "SGP": "SG", "SHN": "SH",
    "SVN": "SI", "SJM": "SJ", "SVK": "SK", "SLE": "SL", "SMR": "SM", "SEN": "SN", "SOM": "SO",
    "SUR": "SR", "SSD": "SS", "STP": "ST", "SLV": "SV", "SXM": "SX", "SYR": "SY", "SWZ": "SZ",
    "TCA": "TC", "TCD": "TD", "ATF": "TF", "TGO": "TG", "THA": "TH", "TJK": "TJ", "TKL": "TK",
    "TLS": "TL", "TKM": "TM", "TUN": "TN", "TON": "TO", "TUR": "TR", "TTO": "TT", "TUV": "TV",
    "TWN": "TW", "TZA": "TZ",
    "UKR": "UA", "UGA": "UG", "UMI": "UM", "USA": "US", "URY": "UY", "UZB": "UZ",
    "VAT": "VA", "VCT": "VC", "VEN": "VE", "VGB": "VG", "VIR": "VI", "VNM": "VN", "VUT": "VU",
    "WLF": "WF", "WSM": "WS",
    "YEM": "YE", "MYT": "YT",
    "ZAF": "ZA", "ZMB": "ZM", "ZWE": "ZW",
}

_INFORMAL_CODES: dict[str, str] = {
    "UK": "GB", "EN": "GB", "ENG": "GB", "EL": "GR", "UAE": "AE", "KSA": "SA", "GER": "DE",
    "JAP": "JP", "PRC": "CN", "ROK": "KR", "DPRK": "KP", "DRC": "CD", "RSA": "ZA", "HOL": "NL",
    "NED": "NL", "SUI": "CH", "POR": "PT", "DEN": "DK", "GRE": "GR", "CRO": "HR", "BUL": "BG",
    "ROM": "RO", "SLO": "SI", "LAT": "LV", "IRE": "IE", "VIE": "VN", "PHI": "PH", "INA": "ID",
    "MAS": "MY", "TPE": "TW", "NGR": "NG", "ALG": "DZ", "URU": "UY", "PAR": "PY", "CRC": "CR",
    "KUW": "KW", "IRI": "IR", "ZIM": "ZW", "SRI": "LK", "NEP": "NP", "MGL": "MN", "BAN": "BD",
}

ALIASES: dict[str, str] = {**_ALPHA3_TO_ALPHA2, **_INFORMAL_CODES}

_NAME_ALIASES: dict[str, str] = {
    "United States of America": "US",
    "America": "US",
    "Соединённые Штаты Америки": "US",
    "Соединённые Штаты": "US",
    "Америка": "US",
    "United Kingdom of Great Britain and Northern Ireland": "GB",
    "Great Britain": "GB",
    "Britain": "GB",
    "England": "GB",
    "Соединённое Королевство": "GB",
    "Британия": "GB",
    "Англия": "GB",
    "Russian Federation": "RU",
    "Российская Федерация": "RU",
    "РФ": "RU",
    "Emirates": "AE",
    "Объединённые Арабские Эмираты": "AE",
    "Эмираты": "AE",
    "Czech Republic": "CZ",
    "Чешская Республика": "CZ",
    "The Netherlands": "NL",
    "Netherlands, Kingdom of the": "NL",
    "Holland": "NL",
    "Голландия": "NL",
    "Korea": "KR",
    "Republic of Korea": "KR",
    "Korea, Republic of": "KR",
    "Корея": "KR",
    "Республика Корея": "KR",
    "Korea, Democratic People's Republic of": "KP",
    "КНДР": "KP",
    "People's Republic of China": "CN",
    "КНР": "CN",
    "Türkiye": "TR",
    "Côte d'Ivoire": "CI",
    "Берег Слоновой Кости": "CI",
    "Cabo Verde": "CV",
    "Острова Зелёного Мыса": "CV",
    "Democratic Republic of the Congo": "CD",
    "Congo, Democratic Republic of the": "CD",
    "Congo-Kinshasa": "CD",
    "Демократическая Республика Конго": "CD",
    "ДРК": "CD",
    "Congo-Brazzaville": "CG",
    "Центральноафриканская Республика": "CF",
    "Южная Африка": "ZA",
    "Южно-Африканская Республика": "ZA",
    "Burma": "MM",
    "Бирма": "MM",
    "Swaziland": "SZ",
    "Свазиленд": "SZ",
    "Macedonia": "MK",
    "Македония": "MK",
    "East Timor": "TL",
    "Тимор-Лесте": "TL",
    "Vatican": "VA",
    "Holy See": "VA",
    "Святой Престол": "VA",
    "Macao": "MO",
    "Hongkong": "HK",
    "Viet Nam": "VN",
    "Brunei Darussalam": "BN",
    "Lao People's Democratic Republic": "LA",
    "Syrian Arab Republic": "SY",
    "Iran, Islamic Republic of": "IR",
    "Moldova, Republic of": "MD",
    "Молдавия": "MD",
    "Tanzania, United Republic of": "TZ",
    "Venezuela, Bolivarian Republic of": "VE",
    "Bolivia, Plurinational State of": "BO",
    "Taiwan, Province of China": "TW",
    "Palestine, State of": "PS",
    "State of Palestine": "PS",
    "Palestinian Territories": "PS",
    "Палестинские территории": "PS",
    "Micronesia, Federated States of": "FM",
    "Federated States of Micronesia": "FM",
    "Virgin Islands, British": "VG",
    "Virgin Islands, U.S.": "VI",
    "United States Virgin Islands": "VI",
    "U.S. Outlying Islands": "UM",
    "Falkland Islands (Malvinas)": "FK",
    "Falklands": "FK",
    "Фолкленды": "FK",
    "Cocos (Keeling) Islands": "CC",
    "Keeling Islands": "CC",
    "Pitcairn": "PN",
    "Питкэрн": "PN",
    "Bonaire, Sint Eustatius and Saba": "BQ",
    "Бонайре, Синт-Эстатиус и Саба": "BQ",
    "Saint Helena, Ascension and Tristan da Cunha": "SH",
    "Saint Martin (French part)": "MF",
    "Sint Maarten (Dutch part)": "SX",
    "Åland": "AX",
    "Белоруссия": "BY",
    "Кыргызстан": "KG",
    "Kyrgyz Republic": "KG",
    "Туркмения": "TM",
    "Тайланд": "TH",
    "Slovak Republic": "SK",
    "Bosnia": "BA",
    "Босния": "BA",
    "Доминикана": "DO",
    "Эль-Сальвадор": "SV",
    "Багамские острова": "BS",
    "Бермуды": "BM",
    "Сейшельские острова": "SC",
    "Коморские острова": "KM",
    "Мальдивские острова": "MV",
    "Кайманы": "KY",
    "Фареры": "FO",
    "Острова Теркс и Кайкос": "TC",
    "Французские Южные и Антарктические территории": "TF",
    "Hong Kong SAR China": "HK",
    "Гонконг (САР)": "HK",
    "Macao SAR China": "MO",
    "Макао (САР)": "MO",
    "Myanmar (Burma)": "MM",
    "Мьянма (Бирма)": "MM",
    "Конго - Киншаса": "CD",
    "Конго - Браззавиль": "CG",
    "Центрально-Африканская Республика": "CF",
    "Федеративные Штаты Микронезии": "FM",
    "Бонэйр, Синт-Эстатиус и Саба": "BQ",
    "Острова Кайман": "KY",
    "Острова Херд и Макдональд": "HM",
    "Heard & McDonald Islands": "HM",
    "Остров Св. Елены": "SH",
    "Виргинские острова (Великобритания)": "VG",
    "South Georgia & South Sandwich Islands": "GS",
    "St. Vincent & Grenadines": "VC",
    "Saint Vincent": "VC",
    "Сент-Винсент": "VC",
}

_DROPPED_CHARS = "\"'`´«»„“”‘’‹›." + "​‌‍⁠﻿"
_DROP_TABLE = {ord(char): None for char in _DROPPED_CHARS}
_HARD_SEPARATORS_RE = re.compile(r"[,;|/\n\r\t]+")
_NON_WORD_RE = re.compile(r"[\W_]+")
_SAINT_RE = re.compile(r"\bst (?=\w)")
_ISLANDS_RE = re.compile(r"\bо-в(а?)\b")


def _strip_latin_diacritics(text: str) -> str:
    """Убирает диакритику у латинских букв («Curaçao» → «Curacao»), кириллицу не трогает."""
    result = []
    for char in text:
        base = unicodedata.normalize("NFKD", char)[0]
        result.append(base if base.isascii() and base.isalpha() else char)
    return "".join(result)


def _name_key(text: str) -> str:
    """Приводит название к ключу сравнения: регистр, «ё/е», диакритика, пунктуация, пробелы."""
    text = unicodedata.normalize("NFC", text).casefold().replace("ё", "е")
    text = _ISLANDS_RE.sub(lambda match: "острова" if match.group(1) else "остров", text)
    text = _strip_latin_diacritics(text).translate(_DROP_TABLE).replace("&", " and ")
    text = _NON_WORD_RE.sub(" ", text).strip()
    return _SAINT_RE.sub("saint ", text)


def _build_name_index() -> dict[str, str]:
    """Строит индекс «ключ названия → код»; официальные названия важнее синонимов."""
    index: dict[str, str] = {}
    for code, names in COUNTRIES.items():
        for name in names:
            index[_name_key(name)] = code
    for name, code in _NAME_ALIASES.items():
        index.setdefault(_name_key(name), code)
    return index


def _build_search_index() -> list[tuple[str, tuple[str, ...]]]:
    """Собирает для каждого кода все ключи названий; порядок — по алфавиту кода."""
    keys: dict[str, list[str]] = {code: [] for code in sorted(COUNTRIES)}
    for key, code in _NAME_INDEX.items():
        keys[code].append(key)
    return [(code, tuple(names)) for code, names in keys.items()]


_NAME_INDEX = _build_name_index()
_SEARCH_INDEX = _build_search_index()
_MAX_NAME_WORDS = max(len(key.split()) for key in _NAME_INDEX)


def normalize_country_code(raw: str) -> str | None:
    """Возвращает код alpha-2 для кода, алиаса или названия страны; иначе None.

    Порядок: точный код alpha-2 → ALIASES (alpha-3, "UK", "USA"…) → полное название
    на русском или английском без учёта регистра, «ё/е», кавычек, точек и дефисов.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.translate(_DROP_TABLE).strip()
    if not cleaned:
        return None
    if cleaned.isascii():
        token = cleaned.upper()
        if token in COUNTRIES:
            return token
        if token in ALIASES:
            return ALIASES[token]
    return _NAME_INDEX.get(_name_key(raw))


def _match_longest(words: list[str], start: int) -> tuple[str | None, int]:
    """Ищет самое длинное название или код, начинающиеся со слова `start`."""
    longest = min(len(words) - start, _MAX_NAME_WORDS)
    for length in range(longest, 0, -1):
        code = normalize_country_code(" ".join(words[start:start + length]))
        if code:
            return code, length
    return None, 1


def parse_geo_input(raw: str) -> tuple[list[str], list[str]]:
    """Разбирает ввод вида "MX, au;ro  UK Австралия" на коды и нераспознанные токены.

    Сначала ввод делится по «жёстким» разделителям (запятая, точка с запятой, перевод
    строки, табуляция, "|", "/"), и кусок распознаётся целиком — так выживают названия
    из нескольких слов («Южная Корея», «United States»). Нераспознанный кусок делится
    по пробелам, при этом соседние слова склеиваются, пока дают название страны.
    Возвращает (коды без дублей в порядке ввода, нераспознанные токены без дублей).
    """
    codes: list[str] = []
    unknown: list[str] = []
    if not isinstance(raw, str):
        return codes, unknown
    for chunk in _HARD_SEPARATORS_RE.split(raw):
        words = chunk.split()
        position = 0
        while position < len(words):
            code, length = _match_longest(words, position)
            word = words[position]
            if code is not None:
                if code not in codes:
                    codes.append(code)
            elif word not in unknown and any(char.isalnum() for char in word):
                unknown.append(word)
            position += length
    return codes, unknown


def country_name(code: str, lang: str = "ru") -> str:
    """Возвращает название страны по коду alpha-2; если кода нет в справочнике — сам код.

    lang: "ru" (по умолчанию) или "en".
    """
    names = COUNTRIES.get(code.strip().upper()) if isinstance(code, str) else None
    if names is None:
        return code
    return names[0] if str(lang).lower().startswith("en") else names[1]


def _as_item(code: str) -> dict[str, str]:
    """Собирает элемент выдачи автокомплита."""
    name_en, name_ru = COUNTRIES[code]
    return {"code": code, "name_en": name_en, "name_ru": name_ru}


def search_countries(query: str, limit: int = 20) -> list[dict[str, str]]:
    """Ищет страны для автокомплита по коду или названию (ru/en, с учётом синонимов).

    Порядок выдачи: точное совпадение (код, алиас или полное название) → префикс кода →
    префикс названия → вхождение в название; внутри группы — по алфавиту кода.
    Пустой запрос возвращает первые `limit` стран по алфавиту кода.
    """
    if limit <= 0:
        return []
    key = _name_key(query) if isinstance(query, str) else ""
    if not key:
        return [_as_item(code) for code, _ in _SEARCH_INDEX[:limit]]
    exact = normalize_country_code(query)
    ranked: list[tuple[int, str]] = []
    for code, names in _SEARCH_INDEX:
        if code == exact:
            rank = 0
        elif code.casefold().startswith(key):
            rank = 1
        elif any(name.startswith(key) for name in names):
            rank = 2
        elif any(key in name for name in names):
            rank = 3
        else:
            continue
        ranked.append((rank, code))
    ranked.sort()
    return [_as_item(code) for _, code in ranked[:limit]]
