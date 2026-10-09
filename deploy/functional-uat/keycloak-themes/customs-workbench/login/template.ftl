<#import "footer.ftl" as loginFooter>
<#macro registrationLayout bodyClass="" displayInfo=false displayMessage=true displayRequiredFields=false>
<!DOCTYPE html>
<html class="${properties.kcHtmlClass!}" lang="${lang}"<#if realm.internationalizationEnabled> dir="${(locale.rtl)?then('rtl','ltr')}"</#if>>

<head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta name="robots" content="noindex, nofollow">

    <#if properties.meta?has_content>
        <#list properties.meta?split(' ') as meta>
            <meta name="${meta?split('==')[0]}" content="${meta?split('==')[1]}"/>
        </#list>
    </#if>
    <title>${msg("loginTitle",(realm.displayName!''))}</title>
    <#-- customs-workbench: 문서 간 View Transition 사용 설정은 링크된 CSS에 두면 Keycloak 화면이
         나타나는 시점에 아직 반영되지 않아 전환이 취소됩니다(Chrome "opt-in disabled"). 그래서 head에 인라인으로 둡니다. -->
    <style>
/* 앱 로그인 화면 → Keycloak 로그인 창 전환(같은 origin의 문서 간 View Transition).
   왼쪽 소개 영역은 이름을 붙여 제자리에 두고, 오른쪽만 살짝 떠오르며 바뀝니다.
   지원하지 않는 브라우저는 그냥 페이지가 바뀝니다. 앱 components.css와 Keycloak 테마 CSS에 같은 블록이 있습니다. */
@view-transition { navigation: auto; }
.login-hero { view-transition-name: login-hero; }
::view-transition-old(root) { animation: .18s ease-in both login-vt-out; }
::view-transition-new(root) { animation: .32s cubic-bezier(.2, .7, .2, 1) both login-vt-in; }
::view-transition-group(login-hero) { animation-duration: 0s; }
@keyframes login-vt-out { to { opacity: 0; } }
@keyframes login-vt-in { from { opacity: 0; transform: translateY(10px); } }
@media (prefers-reduced-motion: reduce) {
  @view-transition { navigation: none; }
}
    </style>

    <link rel="icon" href="${url.resourcesPath}/img/favicon.ico" />
    <#if properties.stylesCommon?has_content>
        <#list properties.stylesCommon?split(' ') as style>
            <link href="${url.resourcesCommonPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>
    <#if properties.styles?has_content>
        <#list properties.styles?split(' ') as style>
            <link href="${url.resourcesPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>
    <#if properties.scripts?has_content>
        <#list properties.scripts?split(' ') as script>
            <script src="${url.resourcesPath}/${script}" type="text/javascript"></script>
        </#list>
    </#if>
    <script type="importmap">
        {
            "imports": {
                "rfc4648": "${url.resourcesCommonPath}/vendor/rfc4648/rfc4648.js"
            }
        }
    </script>
    <script src="${url.resourcesPath}/js/menu-button-links.js" type="module"></script>
    <#if scripts??>
        <#list scripts as script>
            <script src="${script}" type="text/javascript"></script>
        </#list>
    </#if>
    <script type="module">
        import { startSessionPolling } from "${url.resourcesPath}/js/authChecker.js";

        startSessionPolling(
            "${url.ssoLoginInOtherTabsUrl?no_esc}"
        );
    </script>
    <script type="module">
        document.addEventListener("click", (event) => {
            const link = event.target.closest("a[data-once-link]");

            if (!link) {
                return;
            }

            if (link.getAttribute("aria-disabled") === "true") {
                event.preventDefault();
                return;
            }

            const { disabledClass } = link.dataset;

            if (disabledClass) {
                link.classList.add(...disabledClass.trim().split(/\s+/));
            }

            link.setAttribute("role", "link");
            link.setAttribute("aria-disabled", "true");
        });
    </script>
    <#if authenticationSession??>
        <script type="module">
            import { checkAuthSession } from "${url.resourcesPath}/js/authChecker.js";

            checkAuthSession(
                "${authenticationSession.authSessionIdHash}"
            );
        </script>
    </#if>
</head>

<body class="${properties.kcBodyClass!}" data-page-id="login-${pageId}">
<#-- customs-workbench: 앱 로그인 화면(customs-frontend src/login.ts)과 같은 2단 배치.
     왼쪽 소개는 고정 예시값이고, 오른쪽에 Keycloak 기본 폼을 그대로 둡니다. -->
<div class="login-screen">
<section class="login-hero">
  <div class="login-hero__brand">
    <div class="login-hero__mark" aria-hidden="true"></div>
    <div><strong>통관 AI 사전심사</strong><small>Customs-Fit 업무시스템</small></div>
  </div>
  <div class="login-hero__copy">
    <p class="login-hero__headline">수입 통관 서류,<br>AI가 먼저 살피고<br>담당자가 확정합니다.</p>
    <p>서류 대조와 근거 찾기는 AI가 먼저 끝내 둡니다. 담당자는 판단에만 집중하세요.</p>
  </div>
  <div class="login-preview" aria-hidden="true">
    <div class="login-preview__top"><span class="login-preview__dots"><i></i><i></i><i></i></span><b>심사 작업대</b><span>예시 Case · 냉동 새우</span></div>
    <div class="login-preview__body">
      <div class="login-preview__docs">
        <div><b>B/L</b><span>추출 완료</span></div>
        <div><b>Invoice</b><span>추출 완료</span></div>
        <div><b>P/L</b><span>추출 완료</span></div>
        <div><b>C/O</b><span>추출 완료</span></div>
      </div>
      <div class="login-preview__row"><span>교차검증</span><b>6개 필드 대조</b><em class="is-bad">중량 불일치 1건</em></div>
      <div class="login-preview__row"><span>HS 분류</span><b>0306.17-0000</b><em class="is-ok">근거 3건</em></div>
      <div class="login-preview__row"><span>통관 요건</span><b>검역 대상 품목</b><em class="is-warn">확인 필요</em></div>
      <div class="login-preview__verdict"><div><b>AI 권고 · 검토 필요</b><small>불일치 1건과 요건 1건을 담당자가 확인해 주세요.</small></div><span>검토 열기</span></div>
    </div>
  </div>
  <ul class="login-hero__feats">
    <li><b>서류 자동 추출</b><span>양식이 달라도 품목·중량·금액을 정형 데이터로 정리</span></li>
    <li><b>서류 간 교차검증</b><span>같은 항목의 값이 서류마다 다르면 바로 표시</span></li>
    <li><b>근거 있는 권고</b><span>HS 분류와 요건마다 법령·해설서 출처 연결</span></li>
  </ul>
</section>
<main class="login-side">
<nav class="login-side__top"><a href="#">이용 안내</a><a href="#">문의</a></nav>
<div class="${properties.kcLoginClass!}">
    <div id="kc-header" class="${properties.kcHeaderClass!}">
        <div id="kc-header-wrapper"
             class="${properties.kcHeaderWrapperClass!}">${kcSanitize(msg("loginTitleHtml",(realm.displayNameHtml!'')))?no_esc}</div>
    </div>
    <div class="${properties.kcFormCardClass!}">
        <header class="${properties.kcFormHeaderClass!}">
            <#if realm.internationalizationEnabled  && locale.supported?size gt 1>
                <div class="${properties.kcLocaleMainClass!}" id="kc-locale">
                    <div id="kc-locale-wrapper" class="${properties.kcLocaleWrapperClass!}">
                        <div id="kc-locale-dropdown" class="menu-button-links ${properties.kcLocaleDropDownClass!}">
                            <button tabindex="1" id="kc-current-locale-link" aria-label="${msg("languages")}" aria-haspopup="true" aria-expanded="false" aria-controls="language-switch1">${locale.current}</button>
                            <ul role="menu" tabindex="-1" aria-labelledby="kc-current-locale-link" aria-activedescendant="" id="language-switch1" class="${properties.kcLocaleListClass!}">
                                <#assign i = 1>
                                <#list locale.supported as l>
                                    <li class="${properties.kcLocaleListItemClass!}" role="none">
                                        <a role="menuitem" id="language-${i}" class="${properties.kcLocaleItemClass!}" href="${l.url}">${l.label}</a>
                                    </li>
                                    <#assign i++>
                                </#list>
                            </ul>
                        </div>
                    </div>
                </div>
            </#if>
        <#if !(auth?has_content && auth.showUsername() && !auth.showResetCredentials())>
            <#if displayRequiredFields>
                <div class="${properties.kcContentWrapperClass!}">
                    <div class="${properties.kcLabelWrapperClass!} subtitle">
                        <span class="subtitle"><span class="required">*</span> ${msg("requiredFields")}</span>
                    </div>
                    <div class="col-md-10">
                        <h1 id="kc-page-title"><#nested "header"></h1>
                    </div>
                </div>
            <#else>
                <h1 id="kc-page-title"><#nested "header"></h1>
            </#if>
        <#else>
            <#if displayRequiredFields>
                <div class="${properties.kcContentWrapperClass!}">
                    <div class="${properties.kcLabelWrapperClass!} subtitle">
                        <span class="subtitle"><span class="required">*</span> ${msg("requiredFields")}</span>
                    </div>
                    <div class="col-md-10">
                        <#nested "show-username">
                        <div id="kc-username" class="${properties.kcFormGroupClass!}">
                            <label id="kc-attempted-username">${auth.attemptedUsername}</label>
                            <a id="reset-login" href="${url.loginRestartFlowUrl}" aria-label="${msg("restartLoginTooltip")}">
                                <div class="kc-login-tooltip">
                                    <i class="${properties.kcResetFlowIcon!}"></i>
                                    <span class="kc-tooltip-text">${msg("restartLoginTooltip")}</span>
                                </div>
                            </a>
                        </div>
                    </div>
                </div>
            <#else>
                <#nested "show-username">
                <div id="kc-username" class="${properties.kcFormGroupClass!}">
                    <label id="kc-attempted-username">${auth.attemptedUsername}</label>
                    <a id="reset-login" href="${url.loginRestartFlowUrl}" aria-label="${msg("restartLoginTooltip")}">
                        <div class="kc-login-tooltip">
                            <i class="${properties.kcResetFlowIcon!}"></i>
                            <span class="kc-tooltip-text">${msg("restartLoginTooltip")}</span>
                        </div>
                    </a>
                </div>
            </#if>
        </#if>
      </header>
      <div id="kc-content">
        <div id="kc-content-wrapper">

          <#-- App-initiated actions should not see warning messages about the need to complete the action -->
          <#-- during login.                                                                               -->
          <#if displayMessage && message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
              <div class="alert-${message.type} ${properties.kcAlertClass!} pf-m-<#if message.type = 'error'>danger<#else>${message.type}</#if>">
                  <div class="pf-c-alert__icon">
                      <#if message.type = 'success'><span class="${properties.kcFeedbackSuccessIcon!}"></span></#if>
                      <#if message.type = 'warning'><span class="${properties.kcFeedbackWarningIcon!}"></span></#if>
                      <#if message.type = 'error'><span class="${properties.kcFeedbackErrorIcon!}"></span></#if>
                      <#if message.type = 'info'><span class="${properties.kcFeedbackInfoIcon!}"></span></#if>
                  </div>
                      <span class="${properties.kcAlertTitleClass!}">${kcSanitize(message.summary)?no_esc}</span>
              </div>
          </#if>

          <#nested "form">

          <#if auth?has_content && auth.showTryAnotherWayLink()>
              <form id="kc-select-try-another-way-form" action="${url.loginAction}" method="post">
                  <div class="${properties.kcFormGroupClass!}">
                      <input type="hidden" name="tryAnotherWay" value="on"/>
                      <a href="#" id="try-another-way"
                         onclick="document.forms['kc-select-try-another-way-form'].requestSubmit();return false;">${msg("doTryAnotherWay")}</a>
                  </div>
              </form>
          </#if>

          <#nested "socialProviders">

          <#if displayInfo>
              <div id="kc-info" class="${properties.kcSignUpClass!}">
                  <div id="kc-info-wrapper" class="${properties.kcInfoAreaWrapperClass!}">
                      <#nested "info">
                  </div>
              </div>
          </#if>
        </div>
      </div>

      <@loginFooter.content/>
    </div>
    <ul class="login-notes">
      <li class="login-notes__ai">AI는 권고만 하고, 최종 판단은 항상 담당자가 확정합니다.</li>
      <li class="login-notes__help">계정 문제는 시스템 관리자에게 문의하십시오.</li>
    </ul>
  </div>
<footer class="login-side__foot"><span>© Customs-Fit</span><a href="#">개인정보 처리방침</a></footer>
</main>
</div>
</body>
</html>
</#macro>
