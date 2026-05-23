# 🚀 Staking Platform BSC

Platformă de staking pe Binance Smart Chain cu APY 50% (prima lună) și 10% ulterior.

## 📋 Specificații

| Parametru | Valoare |
|---|---|
| Token | `0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978` |
| APY Luna 1 | 50% |
| APY Standard | 10% |
| Unlock Period | 7 zile |
| Recompense | Același token |

---

## 🛠️ Instalare & Setup

### Pasul 1 – Clonează repo-ul
```bash
git clone https://github.com/USER/staking-platform.git
cd staking-platform
```

### Pasul 2 – Configurează .env
```bash
cp .env.example .env
# Editează .env și completează:
# PRIVATE_KEY = cheia privată a wallet-ului tău
# BSCSCAN_API_KEY = de pe bscscan.com/apis
```

### Pasul 3 – Instalează dependințele
```bash
# Contract + Hardhat
npm install

# Backend
cd backend && npm install && cd ..

# Frontend
cd frontend && npm install && cd ..
```

---

## 🔗 Deploy Smart Contract

### 1. Deploy pe Testnet (recomandat mai întâi!)
```bash
# Ia BNB de test de la: https://testnet.binance.org/faucet-smart
npm run deploy:testnet
```

### 2. Deploy pe Mainnet
```bash
npm run deploy:mainnet
```

### 3. Verifică contractul pe BscScan
```bash
npx hardhat verify --network bscMainnet ADRESA_CONTRACT "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978"
```

### 4. Finanțează pool-ul de recompense
- Du-te pe BscScan → contractul tău → Write Contract
- Aprobă tokenul pentru contract (funcția `approve` pe token)
- Apelează `fundRewards(cantitate)` cu tokens suficienți pentru recompense

> 💡 **Câți tokens ai nevoie?**
> Dacă ai 100.000 tokens staked și APY 50%,
> ai nevoie de ~50.000 tokens/an = ~4.167 tokens/lună în pool

---

## ⚙️ Rulează Backend

### Local
```bash
cd backend
cp ../.env.example .env.local
# Completează STAKING_CONTRACT_ADDRESS în .env
npm run dev
```

### Deploy pe Railway (recomandat)
1. Du-te la [railway.app](https://railway.app)
2. "New Project" → "Deploy from GitHub"
3. Selectează repo-ul tău → folderul `/backend`
4. Adaugă variabilele din `.env` în "Variables"
5. Deploy automat! 🚀

---

## 🖥️ Rulează Frontend

### Local
```bash
cd frontend
cp ../.env.example .env.local
# Completează VITE_STAKING_ADDRESS în .env.local
npm run dev
# Deschide http://localhost:3000
```

### Deploy pe Vercel (GRATUIT)
1. Du-te la [vercel.com](https://vercel.com)
2. "New Project" → Import din GitHub
3. Root Directory: `frontend`
4. Adaugă variabila `VITE_STAKING_ADDRESS` în Environment Variables
5. Deploy! 🎉

---

## 📁 Structura Proiectului

```
staking-platform/
├── contracts/
│   └── StakingPlatform.sol    # Smart contract principal
├── scripts/
│   └── deploy.js              # Script deploy
├── hardhat.config.js
├── backend/
│   ├── server.js              # API Express
│   └── package.json
├── frontend/
│   ├── src/
│   │   ├── App.jsx            # Componenta principală
│   │   ├── components/
│   │   │   ├── StakingDashboard.jsx
│   │   │   └── StatsPanel.jsx
│   │   ├── hooks/
│   │   │   └── useStaking.js  # Logica Web3
│   │   └── utils/
│   │       └── contracts.js   # ABI + utils
│   └── package.json
├── .env.example
└── README.md
```

---

## 🔌 API Endpoints

| Method | Endpoint | Descriere |
|---|---|---|
| GET | `/api/stats` | Statistici globale (TVL, APY, stakers) |
| GET | `/api/user/:address` | Date utilizator (stake, rewards, unlock) |
| GET | `/api/history/:address` | Istoricul tranzacțiilor |
| GET | `/health` | Health check |

---

## ⚠️ Securitate

- **NICIODATĂ** nu pune `PRIVATE_KEY` pe GitHub
- Testează **întotdeauna** pe Testnet înainte de Mainnet
- Verifică contractul pe BscScan după deploy
- Asigură-te că pool-ul de recompense are tokens suficienți

---

## 📞 Funcții Smart Contract

| Funcție | Descriere |
|---|---|
| `stake(amount)` | Stakează tokens |
| `requestUnlock()` | Pornește countdown 7 zile |
| `unstake()` | Retrage după 7 zile |
| `claimRewards()` | Colectează recompensele |
| `emergencyWithdraw()` | Retragere urgentă (fără recompense) |
| `fundRewards(amount)` | [Owner] Adaugă tokens în pool |
| `pause() / unpause()` | [Owner] Pauzează platforma |
