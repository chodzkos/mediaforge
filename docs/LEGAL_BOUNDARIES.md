# Granice prawne i zakres

Ten dokument definiuje, co aplikacja robi i czego świadomie **nie** robi. Jest wiążący dla implementacji (w tym dla Claude Code — patrz `CLAUDE.md`).

## Przeznaczenie

Osobista archiwizacja materiałów edukacyjnych, szkoleniowych i konferencyjnych (wykłady, kursy, webinary, nagrania konferencji), do których użytkownik ma **legalny dostęp**, wraz z transkrypcją, streszczeniem i przeszukiwalnym archiwum.

## Czego aplikacja NIE robi

1. Nie obchodzi DRM (Widevine/PlayReady) ani innych technicznych środków zabezpieczających (TPM).
2. Nie łamie szyfrowania strumieni i nie wyciąga kluczy licencyjnych.
3. Nie automatyzuje omijania zabezpieczeń serwisów — **żadnego headless scrapingu „omijającego 2FA"**, żadnego bota logującego się i zrzucającego chroniony strumień.
4. Nie zakłada, że posiadanie loginu i hasła = prawo do wykonania lokalnej kopii. Regulamin i licencja źródła mogą zabraniać zapisu nawet bez DRM.

## Podejście do logowania

- Domyślnie: `yt-dlp --cookies-from-browser` (sesja, którą użytkownik utworzył sam, logując się normalnie w przeglądarce).
- Alternatywnie: ręczne logowanie użytkownika; aplikacja korzysta z istniejącej sesji, nie automatyzuje jej obchodzenia.
- Sekrety wyłącznie w `keyring` (systemowy magazyn). Nigdy w configu, w repo ani w plikach jawnych. Przycisk „wyczyść sesję i dane logowania".

## Treści zabezpieczone — zachowanie aplikacji

Gdy źródło stosuje DRM/TPM, aplikacja **nie próbuje** go obejść. Pokazuje komunikat:

```
Nie można przechwycić tego materiału. Źródło prawdopodobnie używa
technicznych zabezpieczeń treści. Aplikacja nie obsługuje obchodzenia
DRM ani zabezpieczeń dostępu.
```

(Nagrywanie ekranu treści chronionej i tak zwykle daje czarny obraz przez HDCP/hardware DRM — to nie jest obejście, to ślepy zaułek.)

## Pozycja odłożona — DO USTALENIA

Docelowa polityka wobec treści DRM dotyczy **zakresu legalnego użytku osobistego i komunikatów w UI** (które przypadki wspieramy i jak je sygnalizujemy), a **nie** obchodzenia zabezpieczeń. Obchodzenie TPM pozostaje trwale poza zakresem (dyrektywa 2001/29/WE art. 6; ustawa o prawie autorskim, dozwolony użytek osobisty art. 23 — który nie rozciąga się na łamanie zabezpieczeń). Każda zmiana w tym obszarze wymaga researchu prawnego przed implementacją.

## Komunikat w UI/README

> Aplikacja jest przeznaczona do archiwizacji materiałów, do których masz legalny dostęp. Przed pobraniem/nagraniem sprawdź regulamin źródła i licencję materiału.
