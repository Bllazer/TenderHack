import './App.css'

function App() {
  return (
    <main className="welcome">
      <section className="welcome__card" aria-labelledby="welcome-title">
        <span className="welcome__badge">9–11 октября 2026</span>
        <p className="welcome__eyebrow">TenderHack</p>
        <h1 id="welcome-title">Добро пожаловать<br />на ТендерХак</h1>
        <p className="welcome__description">
          Здесь начинается наш проект.
        </p>
      </section>
    </main>
  )
}

export default App
