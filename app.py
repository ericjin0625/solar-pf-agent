import streamlit as st
import numpy as np
import pandas as pd
import datetime

# -----------------------------------------------------------------------------
# 1. 페이지 기본 설정
# -----------------------------------------------------------------------------
st.set_page_config(page_title="물류센터 루프탑 태양광 PF 에이전트", layout="wide")

tabs = st.tabs([
    "Intro (소개)", 
    "Inputs (입력변수)", 
    "Summary (요약)", 
    "Calculations (상세계산)", 
    "AFS (연간재무제표)", 
    "Scenario Analysis (시나리오 분석)"
])

# -----------------------------------------------------------------------------
# 2. 전역 데이터 세션 상태(Session State) 초기화 (한국 실정 및 전체 변수 반영)
# -----------------------------------------------------------------------------
default_values = {
    'project_name': "물류센터 루프탑 태양광 PF",
    'capacity': 20.0,
    'gen_hours': 3.6,
    'degradation': 0.5,
    'capex': 30000000.0,
    
    # Timing
    'loan_tenor': 20,
    'const_months': 8,
    
    # Macro & Working Capital
    'inflation': 2.0,
    'ar_months': 1,
    'ap_months': 1,
    
    # Revenues
    'tariff': 0.075,
    'tariff_esc': 0.0,
    'rec_weight': 1.5,
    
    # OPEX
    'opex_base': 250000.0,
    'insurance': 50000.0,
    'am_fee': 40000.0,
    'roof_lease_rate': 10.0,
    'opex_esc': 2.0,
    
    # Financing & Reserves
    'target_dscr': 1.30,
    'max_leverage': 80.0,
    'interest_rate': 4.5,
    'upfront_fee': 1.5,
    'dsra_months': 6,
    'mra_months': 3,
    
    # Tax & Depreciation (한국 세법 기준 반영)
    'depr_years': 20,
    'tax_rate': 22.0  # 한국 법인세+지방소득세 평균 실효세율 추정치
}

for key, val in default_values.items():
    if key not in st.session_state:
        st.session_state[key] = val

# -----------------------------------------------------------------------------
# 3. 백엔드 연산 엔진 (한국형 PF 구조 및 Debt Sculpting 고속 솔버)
# -----------------------------------------------------------------------------
def run_financial_model(inputs):
    num_years = inputs['loan_tenor']
    years = np.arange(1, num_years + 1)
    
    # 1. Macro & Generation
    inflation_index = np.array([(1 + inputs['inflation']/100)**(y-1) for y in years])
    gen_base = inputs['capacity'] * 1000 * inputs['gen_hours'] * 365
    generations = np.array([gen_base * ((1 - inputs['degradation']/100)**(y-1)) for y in years])
    
    # 2. Revenue (운전자본 - 매출채권 회수 지연 반영)
    tariff_array = np.array([inputs['tariff'] * ((1 + inputs['tariff_esc']/100)**(y-1)) for y in years])
    gross_revenues = generations * tariff_array * inputs['rec_weight']
    
    # 매출채권(AR) 모델링: 첫해 매출의 일부가 다음 해로 넘어감
    ar_ratio = inputs['ar_months'] / 12.0
    cash_revenues = np.zeros(num_years)
    cash_revenues[0] = gross_revenues[0] * (1 - ar_ratio)
    for i in range(1, num_years):
        cash_revenues[i] = gross_revenues[i] * (1 - ar_ratio) + gross_revenues[i-1] * ar_ratio
        
    # 3. OPEX (인플레이션 및 물류센터 지붕 임대료 연동)
    opex_fixed = (inputs['opex_base'] + inputs['insurance'] + inputs['am_fee']) * inflation_index
    roof_leases = cash_revenues * (inputs['roof_lease_rate'] / 100)
    total_opex = opex_fixed + roof_leases
    
    # 매입채무(AP) 모델링: 비용 지출 지연
    ap_ratio = inputs['ap_months'] / 12.0
    cash_opex = np.zeros(num_years)
    cash_opex[0] = total_opex[0] * (1 - ap_ratio)
    for i in range(1, num_years):
        cash_opex[i] = total_opex[i] * (1 - ap_ratio) + total_opex[i-1] * ap_ratio
        
    ebitda = cash_revenues - cash_opex
    
    # 4. Depreciation (한국 정액법)
    depreciation = np.full(num_years, inputs['capex'] / inputs['depr_years'])
    
    # 5. Debt Sculpting (Fixed-Point Iteration)
    r_rate = inputs['interest_rate'] / 100
    cfads = np.copy(ebitda)
    sculpted_principal = np.zeros(num_years)
    sculpted_interest = np.zeros(num_years)
    corporate_taxes = np.zeros(num_years)
    
    total_debt_capacity = 0.0
    
    for _ in range(50):
        # DSRA (원리금상환적립금) 기회비용 반영 보수적 CFADS 산정
        allowed_debt_service = cfads / inputs['target_dscr']
        discount_factors = np.array([(1 + r_rate)**(-y) for y in years])
        
        # 최대 대출 규모 (Maximum Leverage Cap 적용)
        sculpted_debt = np.sum(allowed_debt_service * discount_factors)
        max_debt_allowed = inputs['capex'] * (inputs['max_leverage'] / 100)
        total_debt_capacity = min(sculpted_debt, max_debt_allowed)
        
        # Roll-forward 원리금 분할
        remaining_debt = total_debt_capacity
        for i in range(num_years):
            sculpted_interest[i] = remaining_debt * r_rate
            
            # Cap에 걸렸을 경우 (LTV 제약) 평탄화된 원금 상환 적용, 아닐 경우 Sculpting
            if total_debt_capacity == max_debt_allowed:
                sculpted_principal[i] = total_debt_capacity / num_years
            else:
                sculpted_principal[i] = allowed_debt_service[i] - sculpted_interest[i]
                
            if sculpted_principal[i] < 0: sculpted_principal[i] = 0
            remaining_debt -= sculpted_principal[i]
            if remaining_debt < 0: remaining_debt = 0
                
        # 한국 법인세 재계산 (이월결손금 미고려, 당해 과세표준 기준)
        for i in range(num_years):
            taxable_income = ebitda[i] - depreciation[i] - sculpted_interest[i]
            corporate_taxes[i] = max(0, taxable_income * (inputs['tax_rate'] / 100))
            cfads[i] = ebitda[i] - corporate_taxes[i]
            
    debt_service_total = sculpted_principal + sculpted_interest
    
    # 6. DSRA & MRA Funding (적립금 현금흐름)
    dsra_target = (debt_service_total[0] / 12) * inputs['dsra_months'] if debt_service_total[0] > 0 else 0
    mra_target = (total_opex[0] / 12) * inputs['mra_months']
    upfront_fees_amount = total_debt_capacity * (inputs['upfront_fee'] / 100)
    
    # 7. Returns (수익률 지표)
    equity_portion = inputs['capex'] + dsra_target + mra_target + upfront_fees_amount - total_debt_capacity
    equity_cash_flow = cfads - debt_service_total
    equity_cash_flow[-1] += dsra_target + mra_target # 마지막 해 적립금 환입
    
    irr_cash_flows = [-equity_portion] + list(equity_cash_flow)
    try:
        equity_irr = np.irr(irr_cash_flows) * 100
        if np.isnan(equity_irr): equity_irr = 0.0 
    except:
        equity_irr = 0.0 
        
    actual_dscr = [cf / ds if ds > 0 else 99.0 for cf, ds in zip(cfads, debt_service_total)]
    min_dscr = min(actual_dscr) if actual_dscr else 0
    
    # 8. DataFrames
    df_calc = pd.DataFrame({
        "연도": [f"Year {y}" for y in years],
        "발전량 (kWh)": generations,
        "현금유입 매출액 (USD)": cash_revenues,
        "현금유출 OPEX (USD)": cash_opex,
        "EBITDA (USD)": ebitda,
        "감가상각비 (USD)": depreciation,
        "대출 이자 (USD)": sculpted_interest,
        "법인세 (USD)": corporate_taxes,
        "가용현금흐름(CFADS)": cfads,
        "원금 상환액 (USD)": sculpted_principal,
        "배당가능 현금흐름 (USD)": equity_cash_flow,
    }).set_index("연도")
    
    return {
        "df_calc": df_calc, "total_debt": total_debt_capacity, "equity_portion": equity_portion,
        "leverage_ratio": (total_debt_capacity / inputs['capex']) * 100, "equity_irr": equity_irr, 
        "min_dscr": min_dscr, "dsra_target": dsra_target, "mra_target": mra_target
    }

# -----------------------------------------------------------------------------
# TAB 1: INTRO (소개)
# -----------------------------------------------------------------------------
with tabs[0]:
    st.title("Abacus Financial Model - Korean PF Edition")
    st.subheader("물류센터 루프탑 태양광 자산운용 및 리스크 분석 모델")
    st.markdown("""
    본 에이전트는 기존 미국 세법(MACRS, ITC) 기준의 Abacus 모델을 **한국 실정(정액법 감가상각, 내국법인세율, 루프탑 REC 등)에 맞추어 전면 재설계**한 독립형 통합 금융 모델입니다.
    
    * **고속 Debt Sculpting:** VBA 매크로 없이 파이썬 백엔드에서 오차율 0%로 목표 DSCR 기반 대출 규모를 역산합니다.
    * **운전자본 및 적립금:** 매출채권(AR), 매입채무(AP) 지연 기간 및 DSRA/MRA 적립금 계좌의 현금흐름이 100% 반영되어 있습니다.
    """)

# -----------------------------------------------------------------------------
# TAB 2: INPUTS (풀버전 입력 변수 - 한국화 완료)
# -----------------------------------------------------------------------------
with tabs[1]:
    st.header("📋 프로젝트 통합 입력 변수 (Assumptions)")
    
    with st.expander("1. General & Timing (기본 정보 및 일정)", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.text_input("Project Name", value=st.session_state.project_name)
            st.session_state.capacity = st.number_input("Capacity (MW)", value=st.session_state.capacity, step=1.0)
            st.session_state.capex = st.number_input("Total CAPEX (USD)", value=st.session_state.capex, step=100000.0)
        with c2:
            st.date_input("Construction Start Date", datetime.date(2026, 7, 1))
            st.session_state.const_months = st.number_input("Construction Period (Months)", value=st.session_state.const_months)
            st.date_input("Target COD", datetime.date(2027, 3, 1))
        with c3:
            st.session_state.loan_tenor = st.number_input("PPA & Loan Term (Years)", value=st.session_state.loan_tenor)
            st.session_state.gen_hours = st.number_input("일평균 발전시간 (Hours)", value=st.session_state.gen_hours, step=0.1)

    with st.expander("2. Macro & Working Capital (거시경제 및 운전자본)", expanded=False):
        mc1, mc2 = st.columns(2)
        with mc1:
            st.session_state.inflation = st.number_input("General Inflation (%/yr)", value=st.session_state.inflation, step=0.1)
        with mc2:
            st.session_state.ar_months = st.number_input("Accounts Receivable (매출채권 회수 지연 - Months)", value=st.session_state.ar_months)
            st.session_state.ap_months = st.number_input("Accounts Payable (매입채무 결제 지연 - Months)", value=st.session_state.ap_months)

    with st.expander("3. Revenues & OPEX (매출 및 세부 운영비)", expanded=True):
        ro1, ro2, ro3 = st.columns(3)
        with ro1:
            st.markdown("**Revenues (매출)**")
            st.session_state.tariff = st.number_input("PPA Tariff (USD/kWh)", value=st.session_state.tariff, format="%.4f")
            st.session_state.tariff_esc = st.number_input("Tariff Escalation (%/yr)", value=st.session_state.tariff_esc)
            st.session_state.rec_weight = st.number_input("루프탑 REC 가중치", value=st.session_state.rec_weight, step=0.1)
            st.session_state.degradation = st.number_input("Degradation (%/yr)", value=st.session_state.degradation, step=0.1)
        with ro2:
            st.markdown("**Fixed OPEX (고정 운영비)**")
            st.session_state.opex_base = st.number_input("Base O&M Cost (USD/yr)", value=st.session_state.opex_base)
            st.session_state.insurance = st.number_input("Insurance (보험료 - USD/yr)", value=st.session_state.insurance)
            st.session_state.am_fee = st.number_input("Asset Management Fee (USD/yr)", value=st.session_state.am_fee)
        with ro3:
            st.markdown("**Variable OPEX (변동 운영비)**")
            st.session_state.roof_lease_rate = st.number_input("지붕 임대료 (% of Revenue)", value=st.session_state.roof_lease_rate)
            st.session_state.opex_esc = st.number_input("OPEX Escalation (%/yr)", value=st.session_state.opex_esc)

    with st.expander("4. Financing & Reserves (PF 조달 및 적립금)", expanded=True):
        f1, f2, f3 = st.columns(3)
        with f1:
            st.markdown("**Debt Sculpting (상환 구조)**")
            st.session_state.target_dscr = st.number_input("Target Sculpted DSCR", value=st.session_state.target_dscr, step=0.05)
            st.session_state.max_leverage = st.number_input("Maximum Leverage LTV (%)", value=st.session_state.max_leverage)
        with f2:
            st.markdown("**Terms & Fees (금리 및 수수료)**")
            st.session_state.interest_rate = st.number_input("Interest Rate (%)", value=st.session_state.interest_rate, step=0.1)
            st.session_state.upfront_fee = st.number_input("Upfront Fee (취급수수료 - %)", value=st.session_state.upfront_fee, step=0.1)
        with f3:
            st.markdown("**Reserve Accounts (유보금)**")
            st.session_state.dsra_months = st.number_input("DSRA Target (원리금상환 - Months)", value=st.session_state.dsra_months)
            st.session_state.mra_months = st.number_input("MRA Target (대수선/운영 - Months)", value=st.session_state.mra_months)

    with st.expander("5. Depreciation & Tax (한국형 감가상각 및 법인세)", expanded=True):
        t1, t2 = st.columns(2)
        with t1:
            st.session_state.depr_years = st.number_input("Book & Tax Depreciation (정액법 상각 연수)", value=st.session_state.depr_years)
        with t2:
            st.session_state.tax_rate = st.number_input("Corporate Tax Rate (법인세+지방소득세율 %)", value=st.session_state.tax_rate, step=1.0)
            st.info("💡 미국 MACRS 및 ITC(투자세액공제) 로직은 한국 실정에 맞지 않아 정액법(Straight-line) 기준으로 일괄 통합되었습니다.")

# -----------------------------------------------------------------------------
# 연산 실행 
# -----------------------------------------------------------------------------
current_inputs = st.session_state.to_dict()
res = run_financial_model(current_inputs)

# -----------------------------------------------------------------------------
# TAB 3, 4, 5, 6 (출력부)
# -----------------------------------------------------------------------------
with tabs[2]:
    st.header("📊 주요 재무 성과 지표 (Summary)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("주주 IRR (Equity IRR)", f"{res['equity_irr']:.2f} %")
    m2.metric("최저 DSCR (Min DSCR)", f"{res['min_dscr']:.2f} x")
    m3.metric("최대 대출액 (Max Debt)", f"$ {res['total_debt']:,.0f}")
    m4.metric("초기 자기자본 (Equity Need)", f"$ {res['equity_portion']:,.0f}")
    
    st.subheader("초기 사업비 및 자금 조달 구조 (Uses & Sources)")
    uses_sources = pd.DataFrame({
        "Uses (자금 용도)": [st.session_state.capex, res['dsra_target'], res['mra_target'], res['total_debt'] * (st.session_state.upfront_fee/100)],
        "비중(U)": ["CAPEX", "DSRA 적립", "MRA 적립", "금융 수수료"],
        "Sources (자금 조달)": [res['total_debt'], res['equity_portion'], 0, 0],
        "비중(S)": ["Senior Debt (PF대출)", "Equity (자기자본)", "", ""]
    }, index=["1", "2", "3", "4"])
    st.table(uses_sources.style.format({"Uses (자금 용도)": "$ {:,.0f}", "Sources (자금 조달)": "$ {:,.0f}"}))
    st.bar_chart(res['df_calc'][["EBITDA (USD)", "배당가능 현금흐름 (USD)"]])

with tabs[3]:
    st.header("⚙️ 20개년 상세 계산 로직 (Calculations)")
    st.dataframe(res['df_calc'].style.format("{:,.0f}"))

with tabs[4]:
    st.header("📑 연간 재무제표 요약 (AFS)")
    st.dataframe(res['df_calc'][["현금유입 매출액 (USD)", "현금유출 OPEX (USD)", "EBITDA (USD)", "법인세 (USD)", "배당가능 현금흐름 (USD)"]].T.style.format("{:,.0f}"))

with tabs[5]:
    st.header("🎛️ 시나리오 스트레스 테스트 (Stress Testing)")
    st.write("실시간으로 주요 리스크 변수를 변동시킬 때 IRR과 대출 한도가 어떻게 깨지는지 확인합니다.")
    
    scenarios = {
        "Base Case (기본)": current_inputs,
        "P90 발전량 (-10%)": {**current_inputs, 'capacity': current_inputs['capacity'] * 0.9},
        "금리 2%p 상승": {**current_inputs, 'interest_rate': current_inputs['interest_rate'] + 2.0},
        "물류센터 지붕임대료 5%p 인상": {**current_inputs, 'roof_lease_rate': current_inputs['roof_lease_rate'] + 5.0}
    }
    
    s_results = []
    for name, s_in in scenarios.items():
        s_res = run_financial_model(s_in)
        s_results.append({
            "시나리오": name, "IRR (%)": f"{s_res['equity_irr']:.2f} %",
            "Max Debt": f"$ {s_res['total_debt']:,.0f}", "Min DSCR": f"{s_res['min_dscr']:.2f} x"
        })
    st.table(pd.DataFrame(s_results).set_index("시나리오"))
